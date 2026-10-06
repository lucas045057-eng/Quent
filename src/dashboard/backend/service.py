from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import threading
import time

from .db import DatabaseReader
from .models import envelope, now, payload, timestamp
from .paper import PaperReader
from quant_realtime_paper.store import SessionStore


class DashboardService:
    def __init__(self, config):
        self.config = config
        self.database = DatabaseReader(config)
        self.paper_reader = PaperReader(config)
        self._cache = None
        self._cached_at = 0
        self._lock = threading.Lock()
        from quant_execution.risk_config import RiskConfigLoader
        self.risk_loader=RiskConfigLoader(Path(os.environ.get('QUANT_RISK_CONFIG_PATH',str(config.root/'config/risk_policy_v2.json'))))

    def sources(self):
        with self._lock:
            if self._cache is None or time.monotonic()-self._cached_at >= 2:
                self._cache = self.database.snapshot()
                self._cached_at = time.monotonic()
            database = self._cache
        # Process liveness is volatile; refresh it for every request even while the
        # slower database snapshot remains within its short cache window.
        return database, self.paper_reader.snapshot()

    def paper(self):
        _, paper = self.sources()
        return envelope(paper, 'AVAILABLE' if paper['currents'] else ('STALE' if paper['sessions'] else 'NO_DATA'),
            [item['source'] for item in paper['sessions']], paper['warnings'])

    def overview(self):
        database, paper = self.sources()
        current = paper['current']
        position = current['position'] if current else {}
        metrics = {key: position.get(source) for key, source in (
            ('equity','equity'), ('available_balance','available_balance'),
            ('unrealized_pnl','unrealized_pnl'), ('realized_pnl','realized_trade_pnl'))}
        metrics.update(total_pnl=None, position_count=None, orders_today=None, trades_today=None)
        if current:
            try:
                parts = [Decimal(str(position[key])) for key in ('unrealized_pnl','realized_trade_pnl','fees','funding_cash')]
                if all(value.is_finite() for value in parts):
                    metrics['total_pnl'] = str(parts[0]+parts[1]-parts[2]+parts[3])
            except (KeyError, InvalidOperation, TypeError):
                pass
            quantity = position.get('quantity', position.get('qty'))
            try:
                metrics['position_count'] = int(Decimal(str(quantity)) != 0)
            except (InvalidOperation, TypeError):
                pass
        core = self.core_status(database)
        warnings = [*database['warnings'], *paper['warnings']]
        if len(paper['currents']) > 1:
            warnings.append('MULTIPLE_ACCOUNTS_SELECT_SESSION')
        return envelope(dict(system=core, mode='PAPER' if paper['currents'] else 'IDLE',
            paper=paper['state'], live='DISABLED', jev='NOT_CONFIGURED',
            funding='ADAPTER_IMPLEMENTED', funding_source='NO_DATA' if not current else 'PAPER_RECORDED',
            database=database['state'], reconciliation=current.get('reconciliation') if current else None,
            metrics=metrics, metric_scope=current['session_id'] if current else None,
            pnl_definition='realized_trade_pnl + unrealized_pnl - fees + funding_cash',
            paper_sessions=len(paper['sessions'])),
            'AVAILABLE' if current else 'NO_DATA', ['verified Paper heartbeat','runtime_health_events'], warnings)

    def core_status(self, database):
        rows = database['tables'].get('runtime_health_events', [])
        if not rows:
            return 'NO_DATA'
        latest_by_component = {}
        for row in rows:
            component = str(row.get('component') or 'UNKNOWN').lower()
            observed = timestamp(row.get('recovered_at')) or timestamp(row.get('created_at'))
            if observed is None:
                continue
            previous = latest_by_component.get(component)
            if previous is None or observed > previous[0]:
                latest_by_component[component] = (observed, str(row.get('state') or 'UNKNOWN').upper())
        current_time = now()
        by_component = {component: state for component, (observed, state) in latest_by_component.items()
            if 0 <= (current_time-observed).total_seconds() <= 5}
        if not by_component:
            return 'STALE'
        severity = {'ERROR': 4, 'FAILED': 4, 'CRITICAL': 4, 'DEGRADED': 3,
            'STOPPED': 3, 'STALE': 3, 'PARTIAL': 2, 'UNKNOWN': 2,
            'RUNNING': 1, 'HEALTHY': 1, 'AVAILABLE': 1}
        worst = max(by_component.values(), key=lambda state: severity.get(state, 2))
        if severity.get(worst, 2) >= 3:
            return worst
        # An individual collector reporting RUNNING cannot establish that every
        # Quant Core component is healthy. Only an explicit system roll-up can.
        if 'system' in by_component and by_component['system'] in {'RUNNING', 'HEALTHY', 'AVAILABLE'}:
            return by_component['system']
        return 'PARTIAL'

    def _sessions(self, paper, session):
        if session:
            matches = [item for item in paper['sessions'] if item['session_id'] == session]
            return matches, 'STALE' if matches and matches[0]['freshness'] != 'CURRENT' else ('AVAILABLE' if matches else 'NO_DATA')
        return paper['currents'], 'AVAILABLE' if paper['currents'] else 'NO_DATA'

    def positions(self, limit=100, session=None):
        _, paper = self.sources()
        sessions, state = self._sessions(paper, session)
        rows = [{**item['position'], 'source':item['source'], 'session_id':item['session_id'],
            'freshness':item['freshness'], 'snapshot_as_of':item['position'].get('as_of'),
            'quantity':item['position'].get('quantity', item['position'].get('qty')),
            'open_time':None, 'stop_loss':None, 'take_profit':None,
            'native_match':None, 'database_match':None} for item in sessions]
        return envelope(dict(items=rows[:limit], scope='SELECTED_SESSION' if session else 'CURRENT'), state,
            [item['source'] for item in sessions], ['UNRECORDED_POSITION_FIELDS_ARE_NULL'])

    def orders(self, limit=100, session=None):
        database, paper = self.sources()
        sessions, state = self._sessions(paper, session)
        intents = database['tables'].get('execution_intents', [])
        results = database['tables'].get('execution_results', [])
        rows, execution_audit = [], []
        scope = 'SELECTED_SESSION' if session else 'CURRENT'
        for item in sessions:
            for client_id, native_status in item['order_states'].items():
                account_id = item['position'].get('account_id')
                intent_row = next((row for row in intents
                    if row.get('client_order_id') == client_id
                    and account_id is not None and str(row.get('account_id')) == str(account_id)
                    and str(row.get('mode') or '').upper() in {'PAPER', 'SANDBOX', 'SIMULATION'}), None)
                intent = payload(intent_row) if intent_row else {}
                matched_results = [row for row in results if intent_row
                    and str(row.get('intent_id')) == str(intent_row.get('intent_id'))
                    and payload(row).get('client_order_id') == client_id]
                result = payload(matched_results[0]) if matched_results else {}
                rows.append(dict(client_order_id=client_id, native_status=native_status,
                    canonical_symbol=item['position'].get('canonical_symbol'), side=intent.get('side'),
                    order_type=intent.get('entry_type'), approved_quantity=intent.get('approved_quantity'),
                    requested_quantity=result.get('requested_quantity'), filled_quantity=result.get('filled_quantity'),
                    remaining_quantity=result.get('remaining_quantity'), average_price=result.get('average_price'),
                    limit_price=None, stop_price_constraint=intent.get('stop_price'),
                    fees=result.get('fees'), event_time=result.get('event_time'),
                    created_at=intent.get('created_at'), source=item['source'],
                    freshness=item['freshness'], session_id=item['session_id']))
                execution_audit.extend({**row, 'session_id': item['session_id'],
                    'freshness': item['freshness'], 'scope': scope} for row in matched_results)
        return envelope(dict(items=rows[:limit], scope=scope,
            execution_audit=execution_audit[:limit]), state, [item['source'] for item in sessions],
            ['EXECUTION_RESULTS_ARE_CUMULATIVE_NOT_PER_FILL'])

    def trades(self, limit=100, session=None):
        return envelope(dict(items=[], exact_fill_records_available=False,
            explanation='NO_EXACT_PER_FILL_TRADE_RECORDS'), 'NO_DATA',
            ['execution_results'], ['CUMULATIVE_RESULTS_DO_NOT_DEFINE_INDIVIDUAL_TRADES'])

    def realtime_paper(self):
        ledger_path = self.config.root / 'var' / 'realtime-paper' / 'sessions.sqlite3'
        snapshot = SessionStore.readonly_snapshot(ledger_path)
        session = snapshot.get('session')
        cycle = snapshot.get('snapshot')
        if session is None:
            return envelope(dict(session=None, runtime=None, decisions=[], events=[]), 'NO_DATA',
                ['operations-only SQLite ledger'], ['REALTIME_PAPER_NOT_STARTED'])
        observed = timestamp((cycle or {}).get('observed_at') or (cycle or {}).get('captured_at'))
        current_time = now()
        age = (current_time - observed).total_seconds() if observed else None
        active = session.get('state') == 'RUNNING' and age is not None and 0 <= age <= 60
        availability = 'AVAILABLE' if active else 'STALE'
        runtime = dict(cycle or {}) if isinstance(cycle, dict) else None
        if runtime is not None:
            health = dict(runtime.get('health') or {})
            process = dict(health.get('process_alive') or {})
            process['status'] = 'ALIVE' if active else ('STOPPED' if session.get('state') != 'RUNNING' else 'STALE')
            process['snapshot_age_seconds'] = age
            health['process_alive'] = process
            heartbeat = dict(health.get('collector_heartbeat') or {})
            heartbeat_at = timestamp(heartbeat.get('checked_at'))
            heartbeat_age = (current_time - heartbeat_at).total_seconds() if heartbeat_at else None
            heartbeat['age_seconds'] = heartbeat_age
            if heartbeat_age is None or heartbeat_age < 0 or heartbeat_age > 30:
                heartbeat['status'] = 'STALE'
            health['collector_heartbeat'] = heartbeat
            market = dict(health.get('fresh_market_data') or {})
            if not active:
                market['status'] = 'STALE'
                market['reason'] = 'REALTIME_PAPER_MONITOR_NOT_ALIVE'
            health['fresh_market_data'] = market
            runtime['health'] = health
        warnings = []
        source = (cycle or {}).get('data_source') or session.get('data_source') or 'UNKNOWN'
        if source != 'REAL_PUBLIC_DATA':
            warnings.append('REAL_PUBLIC_DATA_NOT_CONFIRMED')
        if (cycle or {}).get('readiness') != 'PAPER READY':
            warnings.append('PAPER_NOT_READY')
        data = {
            'session': session,
            'runtime': runtime,
            'decisions': snapshot.get('decisions', []),
            'events': snapshot.get('events', []),
            'snapshot_age_seconds': age,
            'counts': (cycle or {}).get('counts', {}),
            'data_source': source,
        }
        return envelope(data, availability, ['canonical Phase1-8 PostgreSQL via realtime-paper monitor',
            'operations-only SQLite session ledger'], warnings)

    def health(self):
        database, paper = self.sources()
        current = paper['current']
        recorded = database['tables'].get('runtime_health_events', [])
        try:
            rss = int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError, IndexError):
            rss = None
        statuses = dict(quant_core=self.core_status(database), database=database['state'],
            paper=paper['state'], backtest='NO_ACTIVE_RUN',
            nautilus_adapter='PAPER_OBSERVED' if current else 'NO_DATA', funding_adapter='ADAPTER_IMPLEMENTED',
            funding_source='PAPER_RECORDED' if current else 'NO_DATA',
            reconciliation=current.get('reconciliation') if current else 'NO_DATA',
            jev='NOT_CONFIGURED', live='DISABLED')
        return envelope(dict(components=[dict(component=key, status=value,
            source='readonly monitoring') for key, value in statuses.items()],
            database_missing_tables=database['missing'], recorded_health_events=recorded,
            resources=dict(dashboard_rss_bytes=rss, paper_rss_bytes=current.get('rss_bytes') if current else None,
                cpu_percent=None, disk_free_bytes=None, websocket=None, reconnect_count=None,
                degradation_count=None, native_exception_count=None),
            controls='READ_ONLY', process_pid=os.getpid()),
            'PARTIAL' if database['state'] in {'ERROR','PARTIAL'} else 'AVAILABLE',
            ['runtime_health_events','/proc/self/statm'], database['warnings'])

    def risk_policy(self):
        with self._lock:state=self.risk_loader.poll()
        return envelope(dict(status=state.status,new_risk_allowed=state.new_risk_allowed,reason=state.reason,
            config=state.config.model_dump(mode='json',by_alias=True) if state.config else None,
            config_digest=state.config.digest if state.config else None,
            scope='NEW_INTENTS_ONLY',quote_safety_seconds=5,readonly=True),state.status,['local risk config'])

    def strategy_v2(self):
        database,_=self.sources()
        rows=database['tables'].get('strategy_v2_artifacts',[])
        screening_row=next((r for r in rows if r.get('artifact_kind')=='SCREENING'),None)
        screening=payload(screening_row) if screening_row else None
        analyses=[payload(r) for r in rows if r.get('artifact_kind')=='ANALYSIS']
        theses=[payload(r) for r in rows if r.get('artifact_kind')=='THESIS']
        results=[payload(r) for r in rows if r.get('artifact_kind')=='EXECUTION_RESULT']
        latest={}
        for thesis in theses:
            key=(thesis.get('symbol'),thesis.get('horizon'))
            if key not in latest:latest[key]=thesis
        data_source='UNKNOWN'
        if analyses:
            modes={a.get('data_source','UNKNOWN') for a in analyses}
            data_source=next(iter(modes)) if len(modes)==1 else 'MIXED_RECORDED_SOURCES'
        return envelope(dict(strategy_version='QUANT_PAPER_V2',horizons=['1_3H','3_8H','8_24H'],
            timeframes=['15m','1H','4H'],items=screening.get('candidates',[]) if screening else [],
            waiting_triggers=screening.get('waiting_triggers',[]) if screening else [],
            screening_digest=screening_row.get('canonical_digest') if screening_row else None,
            market_digest=screening.get('market_digest') if screening else None,
            generated_at=screening.get('generated_at') if screening else None,
            analyses=analyses,theses=list(latest.values()),execution_results=results,
            data_source=data_source,scope='RECORDED_RESEARCH',live_order_count=None),
            'AVAILABLE' if screening or analyses else 'NO_DATA',['immutable V2 artifacts'],database['warnings'])
