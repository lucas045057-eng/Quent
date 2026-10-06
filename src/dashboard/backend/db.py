"""SELECT-only reader with connection and statement deadlines."""
from contextlib import contextmanager
import time
import threading

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .security import sanitize

# Explicit columns prevent future private columns from becoming public by accident.
TABLES = {
    'strategy_v2_artifacts': ('artifact_id,artifact_kind,canonical_digest,schema_version,payload,created_at','created_at'),
    'strategy_v2_decision_bindings': ('decision_id,evaluation_id,execution_result_digest,policy_digest,recheck_digest,created_at','created_at'),
    'execution_accounts': ('account_id,venue,mode,payload,as_of,lease_expires_at', 'as_of'),
    'execution_intents': ('intent_id,decision_id,evaluation_id,account_id,mode,client_order_id,valid_until,payload', 'valid_until'),
    'execution_results': ('execution_id,intent_id,event_time,payload', 'event_time'),
    'execution_positions': ('content_digest,account_id,canonical_symbol,as_of,payload', 'as_of'),
    'execution_submission_states': ('intent_id,status,updated_at', 'updated_at'),
    'execution_local_events': ('seq,account_id,event_key,event_kind,payload', 'seq'),
    'execution_funding_payments': ('payment_key,account_id,canonical_symbol,boundary,cash,applied,payload', 'boundary'),
    'phase9_evaluations': ('evaluation_id,stage1_candidate_id,symbol,market,timeframe,evaluation_state,reason_code,created_at,updated_at', 'created_at'),
    'phase9_evaluation_snapshots': ('evaluation_id,snapshot_digest,as_of,payload,code_version,created_at', 'created_at'),
    'phase9_evidence_items': ('evidence_id,evaluation_id,evidence_type,semantic_code,source_ref,observed_at,availability_status,payload,created_at', 'created_at'),
    'phase9_evidence_chains': ('chain_id,evaluation_id,input_snapshot_hash,payload,created_at', 'created_at'),
    'phase9_pattern_matches': ('pattern_match_id,evaluation_id,pattern_type,direction,status,pattern_policy_version,payload,created_at', 'created_at'),
    'phase9_jev_reviews': ('review_id,evaluation_id,status,reason_code,provider,model,prompt_version,payload,created_at', 'created_at'),
    'phase9_decision_candidates': ('decision_id,evaluation_id,stage1_candidate_id,symbol,market,timeframe,valid_until,eligible,direction_bias,confidence_band,input_snapshot_hash,jev_review_id,payload,created_at', 'created_at'),
    'phase9_decision_status_events': ('event_id,decision_id,evaluation_id,status,event_time,reason_code,payload', 'event_time'),
    'runtime_health_events': ('id,component,state,outage_started_at,recovered_at,duration_seconds,reason,details,created_at', 'created_at'),
}


class DatabaseReader:
    def __init__(self, config):
        self.config = config
        self._snapshot_lock = threading.Lock()
        self._snapshot_cache = None
        self._snapshot_at = 0

    @contextmanager
    def connect(self):
        with psycopg.connect(self.config.dsn, connect_timeout=2, row_factory=dict_row,
                options='-c default_transaction_read_only=on -c statement_timeout=1500 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=5000') as connection:
            connection.read_only = True
            connection.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(self.config.schema)))
            yield connection

    def read_table(self, connection, table, *, where=None, values=(), limit=200):
        columns, order = TABLES[table]
        if table == 'strategy_v2_artifacts' and where is None:
            # The dashboard renders one current screening batch. Hydrating 200
            # full-market batches duplicates tens of MB on every poll and crowds
            # genuine analysis artifacts out of the bounded research view.
            query = sql.SQL("""
                SELECT * FROM (
                    (SELECT {columns} FROM {schema}.{table}
                     WHERE artifact_kind='SCREENING' ORDER BY created_at DESC LIMIT 1)
                    UNION ALL
                    (SELECT {columns} FROM {schema}.{table}
                     WHERE artifact_kind<>'SCREENING' ORDER BY created_at DESC LIMIT %s)
                ) AS recent_artifacts ORDER BY created_at DESC
            """).format(columns=sql.SQL(columns), schema=sql.Identifier(self.config.schema),
                         table=sql.Identifier(table))
            return sanitize(connection.execute(query, (min(limit, 200),)).fetchall())
        query = sql.SQL('SELECT {} FROM {}.{} {} ORDER BY {} DESC LIMIT %s').format(
            sql.SQL(columns), sql.Identifier(self.config.schema), sql.Identifier(table),
            where or sql.SQL(''), sql.Identifier(order))
        return sanitize(connection.execute(query, (*values, min(limit, 200))).fetchall())

    def snapshot(self):
        # Decisions, events and overview share this reader. Coalesce simultaneous
        # polls instead of decoding the same artifact payload in each worker.
        with self._snapshot_lock:
            if self._snapshot_cache is None or time.monotonic()-self._snapshot_at >= 2:
                self._snapshot_cache = self._read_snapshot()
                self._snapshot_at = time.monotonic()
            return self._snapshot_cache

    def _read_snapshot(self):
        result = dict(state='NOT_CONFIGURED', tables={}, missing=[], warnings=[], transaction_read_only=None)
        if not self.config.dsn:
            return result
        try:
            with self.connect() as connection:
                result['transaction_read_only'] = connection.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] == 'on'
                started = time.monotonic()
                for table in TABLES:
                    if time.monotonic()-started > 3:
                        result['missing'].append(table)
                        result['warnings'].append('DATABASE_READ_BUDGET')
                        continue
                    try:
                        with connection.transaction():
                            result['tables'][table] = self.read_table(connection, table)
                    except psycopg.Error:
                        result['missing'].append(table)
                result['state'] = 'PARTIAL' if result['missing'] else 'CONNECTED'
        except (psycopg.Error, OSError):
            result.update(state='ERROR', tables={}, warnings=['DATABASE_UNAVAILABLE'])
        return result

    def decision_graph(self, decision_id):
        result = dict(state='NOT_CONFIGURED', tables={}, missing=[], warnings=[])
        if not self.config.dsn:
            return result
        try:
            with self.connect() as connection:
                decisions = self.read_table(connection, 'phase9_decision_candidates',
                    where=sql.SQL('WHERE decision_id=%s'), values=(decision_id,), limit=1)
                result['tables']['phase9_decision_candidates'] = decisions
                if not decisions:
                    result['state'] = 'CONNECTED'
                    return result
                evaluation_id = decisions[0]['evaluation_id']
                names = ('phase9_evaluations','phase9_evaluation_snapshots','phase9_evidence_items',
                    'phase9_evidence_chains','phase9_pattern_matches','phase9_jev_reviews',
                    'phase9_decision_status_events','execution_intents')
                for table in names:
                    column, value = ('decision_id', decision_id) if table in {'execution_intents','phase9_decision_status_events'} else ('evaluation_id', evaluation_id)
                    try:
                        with connection.transaction():
                            result['tables'][table] = self.read_table(connection, table,
                                where=sql.SQL('WHERE {}=%s').format(sql.Identifier(column)), values=(value,))
                    except psycopg.Error:
                        result['missing'].append(table)
                intents = result['tables'].get('execution_intents', [])
                try:
                    with connection.transaction():
                        result['tables']['execution_results'] = self.read_table(connection, 'execution_results',
                            where=sql.SQL('WHERE intent_id=ANY(%s::uuid[])'), values=([row['intent_id'] for row in intents],))
                except psycopg.Error:
                    result['missing'].append('execution_results')
                result['state'] = 'PARTIAL' if result['missing'] else 'CONNECTED'
        except (psycopg.Error, OSError):
            result.update(state='ERROR', tables={}, warnings=['DECISION_SOURCE_UNAVAILABLE'])
        return result
