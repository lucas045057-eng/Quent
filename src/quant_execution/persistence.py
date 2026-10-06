"""Durable reservation, single-owner fencing and append-only execution audit."""
from dataclasses import fields
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from psycopg.types.json import Jsonb

from quant_phase9.canonical import canonical_json, canonical_sha256
from quant_execution.contracts import (
    AccountSnapshotV1, ExecutionIntentV1, ExecutionResultV1, PositionSnapshotV1,
    intent_from_json, utc,
)
from quant_execution.risk import RiskPolicyV1


class ReservationRejected(ValueError):
    pass


class FencedOwner(RuntimeError):
    pass


def _payload(value):
    import json
    return json.loads(canonical_json(value))


class ExecutionStore:
    def __init__(self, connection):
        self.conn = connection

    def assert_ready(self):
        required = ('execution_accounts', 'execution_intents', 'execution_results',
            'execution_positions', 'execution_reservations', 'execution_submission_states',
            'execution_local_events', 'execution_funding_payments')
        for name in required:
            if self.conn.execute('SELECT to_regclass(%s)', (name,)).fetchone()[0] is None:
                raise RuntimeError('execution schema is not ready; apply migrations separately')

    def record_account(self, account: AccountSnapshotV1):
        self.conn.execute('''INSERT INTO execution_accounts(account_id,venue,mode,payload,as_of)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT(account_id) DO UPDATE SET
            payload=EXCLUDED.payload,as_of=EXCLUDED.as_of
            WHERE execution_accounts.venue=EXCLUDED.venue AND execution_accounts.mode=EXCLUDED.mode
              AND execution_accounts.as_of <= EXCLUDED.as_of''',
            (account.account_id, account.venue, account.mode, Jsonb(_payload(account)), account.as_of))

    def intent(self, intent_id: UUID) -> ExecutionIntentV1:
        row = self.conn.execute('SELECT payload FROM execution_intents WHERE intent_id=%s', (intent_id,)).fetchone()
        if row is None:
            raise ReservationRejected('INTENT_UNAVAILABLE')
        import json
        return intent_from_json(json.dumps(row[0]))

    def reserve(self, intent: ExecutionIntentV1, policy: RiskPolicyV1, *, now: datetime) -> bool:
        utc(now)
        with self.conn.transaction():
            account = self.conn.execute('SELECT venue,mode,payload,as_of FROM execution_accounts WHERE account_id=%s FOR UPDATE',
                (intent.account_id,)).fetchone()
            if account is None or account[:2] != (intent.venue, intent.mode):
                raise ReservationRejected('ACCOUNT_UNAVAILABLE')
            existing = self.conn.execute('SELECT payload FROM execution_intents WHERE decision_id=%s AND account_id=%s AND mode=%s',
                (intent.decision_id, intent.account_id, intent.mode)).fetchone()
            if existing:
                if existing[0] != _payload(intent):
                    raise ReservationRejected('DECISION_ALREADY_RESERVED')
                return False
            if intent.created_at > now or intent.valid_until <= now:
                raise ReservationRejected('INTENT_EXPIRED')
            if intent.risk_policy_hash != str(canonical_sha256(policy)):
                raise ReservationRejected('POLICY_HASH_MISMATCH')
            decision = self.conn.execute('''SELECT d.evaluation_id,d.symbol,d.eligible,d.direction_bias,
                d.input_snapshot_hash,s.snapshot_digest,d.valid_until,d.payload,
                (SELECT e.status FROM phase9_decision_status_events e WHERE e.decision_id=d.decision_id
                 AND e.event_time<=%s ORDER BY e.event_time DESC,e.created_at DESC,e.event_id DESC LIMIT 1)
                FROM phase9_decision_candidates d JOIN phase9_evaluation_snapshots s
                ON s.evaluation_id=d.evaluation_id WHERE d.decision_id=%s''', (now, intent.decision_id)).fetchone()
            if (decision is None or decision[0] != intent.evaluation_id or decision[1] != intent.core_symbol
                or not decision[2] or decision[3] != ('BULLISH' if intent.side == 'LONG' else 'BEARISH')
                or decision[4] != intent.input_snapshot_hash or decision[5] != intent.evaluation_snapshot_hash
                or decision[6] <= now or decision[8] != 'ACTIVE'):
                raise ReservationRejected('DECISION_UNAVAILABLE')
            value = decision[7].get('value', decision[7])
            if value.get('code_version') != intent.code_version or value.get('decision_policy_version') != intent.decision_policy_version:
                raise ReservationRejected('DECISION_VERSION_MISMATCH')
            state, as_of = account[2], account[3]
            if not 0 <= (now - as_of).total_seconds() <= policy.account_max_age_seconds:
                raise ReservationRejected('STALE_ACCOUNT')
            if state['reconciliation_status'] != 'RECONCILED':
                raise ReservationRejected('ACCOUNT_UNRESOLVED')
            count, notional, margin, risk = self.conn.execute('''SELECT count(*),coalesce(sum(notional),0),
                coalesce(sum(margin),0),coalesce(sum(risk),0) FROM execution_reservations
                WHERE account_id=%s AND state='ACTIVE' ''', (intent.account_id,)).fetchone()
            if policy.risk_config_digest is not None:
                context=active_execution_state(self.conn,account_id=intent.account_id,now=now)
                if context.reconciliation_status!="RECONCILED":raise ReservationRejected("ACCOUNT_UNRESOLVED")
                if len(context.pending_intents)>=policy.max_open_intents:raise ReservationRejected("CONCURRENCY_LIMIT")
                occupied_symbols={p.canonical_symbol for p in context.active_positions}|{i.canonical_symbol for i in context.pending_intents}
                if len(occupied_symbols)>=policy.max_open_positions:
                    raise ReservationRejected("POSITION_LIMIT")
                same=[p for p in context.active_positions if p.canonical_symbol==intent.canonical_symbol]
                same_pending=[i for i in context.pending_intents if i.canonical_symbol==intent.canonical_symbol]
                if same or same_pending:
                    raise ReservationRejected("UNSUPPORTED_EXECUTION_CAPABILITY" if policy.allow_pyramiding or policy.allow_averaging_down else "ADD_POSITION_DISABLED")
                if context.last_flat_at is not None and (now-context.last_flat_at).total_seconds()<policy.cooldown_seconds:
                    raise ReservationRejected("COOLDOWN")
                if max(risk,Decimal(state['reserved_risk']))+intent.risk_budget>policy.max_reserved_risk or intent.risk_budget>policy.max_risk:
                    raise ReservationRejected("RISK_LIMIT")
                if max(notional,Decimal(state['exposure']))+intent.max_notional>policy.max_exposure or intent.max_notional>policy.max_notional:
                    raise ReservationRejected("EXPOSURE_LIMIT")
                if intent.max_margin>context.remaining_margin or intent.max_margin>policy.max_margin:
                    raise ReservationRejected("MARGIN_LIMIT")
                count=0;notional=Decimal('0');margin=Decimal('0');risk=Decimal('0')
                # V2 has already checked the account-wide reservations without double-counting positions.
            if policy.risk_config_digest is None and count >= policy.max_open_intents:
                raise ReservationRejected('CONCURRENCY_LIMIT')
            if policy.risk_config_digest is None and (risk + intent.risk_budget > policy.max_reserved_risk or intent.risk_budget > policy.max_risk):
                raise ReservationRejected('RISK_LIMIT')
            if policy.risk_config_digest is None and (notional + Decimal(state['exposure']) + intent.max_notional > policy.max_exposure or intent.max_notional > policy.max_notional):
                raise ReservationRejected('EXPOSURE_LIMIT')
            if policy.risk_config_digest is None and (margin + intent.max_margin > Decimal(state['available_balance']) or intent.max_margin > policy.max_margin):
                raise ReservationRejected('MARGIN_LIMIT')
            self.conn.execute('''INSERT INTO execution_intents(intent_id,decision_id,evaluation_id,account_id,
                mode,client_order_id,content_digest,valid_until,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (intent.intent_id,intent.decision_id,intent.evaluation_id,intent.account_id,intent.mode,
                 intent.client_order_id,intent.content_digest,intent.valid_until,Jsonb(_payload(intent))))
            self.conn.execute('INSERT INTO execution_reservations(intent_id,account_id,notional,margin,risk) VALUES(%s,%s,%s,%s,%s)',
                (intent.intent_id,intent.account_id,intent.max_notional,intent.max_margin,intent.risk_budget))
            self.conn.execute("INSERT INTO execution_submission_states(intent_id,status,updated_at) VALUES(%s,'RESERVED',%s)", (intent.intent_id,now))
            return True

    def acquire_owner(self, account_id: str, owner: str, *, now: datetime, lease_seconds: int) -> int:
        utc(now)
        if not owner or not 1 <= lease_seconds <= 60:
            raise FencedOwner('invalid ownership lease')
        with self.conn.transaction():
            row = self.conn.execute('SELECT owner_id,owner_epoch,lease_expires_at FROM execution_accounts WHERE account_id=%s FOR UPDATE', (account_id,)).fetchone()
            if row is None or (row[0] not in {None,owner} and row[2] is not None and row[2] > now):
                raise FencedOwner('account already has an execution owner')
            epoch = row[1] if row[0] == owner and row[2] is not None and row[2] > now else row[1] + 1
            self.conn.execute('UPDATE execution_accounts SET owner_id=%s,owner_epoch=%s,lease_expires_at=%s WHERE account_id=%s',
                (owner,epoch,now+timedelta(seconds=lease_seconds),account_id))
            return epoch

    def assert_owner(self, account_id, owner, epoch, *, now):
        row = self.conn.execute('SELECT owner_id,owner_epoch,lease_expires_at FROM execution_accounts WHERE account_id=%s FOR UPDATE', (account_id,)).fetchone()
        if row is None or row[0] != owner or row[1] != epoch or row[2] is None or row[2] <= now:
            raise FencedOwner('execution owner fenced')

    def release_owner(self, account_id, owner, epoch):
        # Explicit release is inactive even if UTC moves backward before restart.
        # Retain the owner identity/epoch; stale releases cannot affect a new owner.
        self.conn.execute('UPDATE execution_accounts SET lease_expires_at=NULL WHERE account_id=%s AND owner_id=%s AND owner_epoch=%s',
            (account_id,owner,epoch))

    def begin_submission(self, intent_id, owner, epoch, *, now):
        with self.conn.transaction():
            intent = self.intent(intent_id)
            self.assert_owner(intent.account_id,owner,epoch,now=now)
            row = self.conn.execute('SELECT status FROM execution_submission_states WHERE intent_id=%s FOR UPDATE', (intent_id,)).fetchone()
            if row[0] != 'RESERVED':
                return False
            if intent.valid_until <= now:
                raise ReservationRejected('INTENT_EXPIRED')
            self.conn.execute("UPDATE execution_submission_states SET status='SUBMITTING',owner_epoch=%s,updated_at=%s WHERE intent_id=%s", (epoch,now,intent_id))
            return True

    def reject_before_submit(self, result, owner, epoch, *, now):
        """Fenced known-no-submit terminal state; never resolve an ambiguous fill."""
        if result.status!='REJECTED' or result.filled_quantity!=0:
            raise ReservationRejected('NO_SUBMIT_RESULT_INVALID')
        with self.conn.transaction():
            intent=self.intent(result.intent_id)
            self.assert_owner(intent.account_id,owner,epoch,now=now)
            row=self.conn.execute('SELECT status,owner_epoch FROM execution_submission_states WHERE intent_id=%s FOR UPDATE',
                (intent.intent_id,)).fetchone()
            if row!=('SUBMITTING',epoch):
                raise ReservationRejected('NO_SUBMIT_STATE_INVALID')
            submitted=self.conn.execute("SELECT 1 FROM execution_local_events WHERE account_id=%s AND event_kind='SUBMIT' AND (intent_id=%s OR (intent_id IS NULL AND payload->>'intent_id'=%s)) LIMIT 1",
                (intent.account_id,intent.intent_id,str(intent.intent_id))).fetchone()
            prior=self.conn.execute('SELECT 1 FROM execution_results WHERE intent_id=%s LIMIT 1',(intent.intent_id,)).fetchone()
            if submitted or prior: raise ReservationRejected('NO_SUBMIT_STATE_AMBIGUOUS')
            self.record_result(result)

    def recover_pending(self, account_id) -> tuple[UUID,...]:
        rows = self.conn.execute('''UPDATE execution_submission_states s SET status='UNKNOWN'
            FROM execution_intents i WHERE s.intent_id=i.intent_id AND i.account_id=%s
            AND s.status IN ('SUBMITTING','UNKNOWN') RETURNING s.intent_id''', (account_id,)).fetchall()
        return tuple(sorted((row[0] for row in rows),key=str))

    def submission_state(self, intent_id):
        return self.conn.execute('SELECT status FROM execution_submission_states WHERE intent_id=%s', (intent_id,)).fetchone()[0]

    def record_result(self, result: ExecutionResultV1):
        with self.conn.transaction():
            # Serialize cumulative fill checks and the mutable submission cursor.
            self.conn.execute('SELECT status FROM execution_submission_states WHERE intent_id=%s FOR UPDATE',
                              (result.intent_id,)).fetchone()
            payload = _payload(result)
            existing = self.conn.execute('SELECT payload FROM execution_results WHERE execution_id=%s',
                                         (result.execution_id,)).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise ReservationRejected('IMMUTABLE_RESULT_CONFLICT')
                return
            self._record_new_result(result, payload)

    def _record_new_result(self, result, payload):
        intent = self.intent(result.intent_id)
        if result.client_order_id != intent.client_order_id or result.requested_quantity != intent.approved_quantity:
            raise ReservationRejected('RESULT_INTENT_MISMATCH')
        cumulative = self.conn.execute('''SELECT max((payload->>'filled_quantity')::numeric)
            FROM execution_results WHERE intent_id=%s''', (result.intent_id,)).fetchone()[0]
        if cumulative is not None and result.filled_quantity < cumulative:
            raise ReservationRejected('DECREASING_FILL')
        self.conn.execute('''INSERT INTO execution_results(execution_id,intent_id,content_digest,event_time,payload)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (result.execution_id,result.intent_id,str(canonical_sha256(result)),result.event_time,Jsonb(payload)))
        existing = self.conn.execute('SELECT payload FROM execution_results WHERE execution_id=%s', (result.execution_id,)).fetchone()
        if existing[0] != payload:
            raise ReservationRejected('IMMUTABLE_RESULT_CONFLICT')
        self.conn.execute('''UPDATE execution_submission_states
            SET status=%s,updated_at=GREATEST(updated_at,%s)
            WHERE intent_id=%s AND (status IN ('SUBMITTING','UNKNOWN') OR updated_at<=%s)''',
            (result.status,result.received_at,result.intent_id,result.received_at))
        if result.status in {'REJECTED','CANCELLED'} and result.filled_quantity == 0:
            self.conn.execute("UPDATE execution_reservations SET state='RELEASED' WHERE intent_id=%s", (result.intent_id,))

    def record_position(self, position: PositionSnapshotV1):
        digest = str(canonical_sha256(position))
        self.conn.execute('''INSERT INTO execution_positions(content_digest,account_id,canonical_symbol,as_of,payload)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (digest,position.account_id,position.canonical_symbol,position.as_of,Jsonb(_payload(position))))

        if position.side=='FLAT' and position.reconciliation_status=='RECONCILED':
            # An entry fill is terminal only after a later reconciled FLAT checkpoint.
            self.conn.execute("""UPDATE execution_reservations r SET state='RELEASED'
                FROM execution_intents i, execution_submission_states s
                WHERE r.intent_id=i.intent_id AND s.intent_id=i.intent_id
                    AND r.account_id=%s AND i.payload->>'canonical_symbol'=%s
                    AND s.status IN ('FILLED','CANCELLED','REJECTED') AND s.updated_at<=%s
                    AND i.payload->>'client_order_id'=ANY(%s)""",
                (position.account_id,position.canonical_symbol,position.as_of,list(position.source_order_ids)))

    def append_local_event(self, *, account_id: str, event_key: str, kind: str, payload: dict, intent_id=None):
        digest = str(canonical_sha256(payload))
        self.conn.execute('''INSERT INTO execution_local_events(account_id,event_key,event_kind,payload,content_digest,intent_id)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (account_id,event_key,kind,Jsonb(payload),digest,intent_id))
        if self.conn.execute('SELECT content_digest FROM execution_local_events WHERE event_key=%s', (event_key,)).fetchone()[0] != digest:
            raise ReservationRejected('LOCAL_EVENT_CONFLICT')

    def iter_local_events(self, account_id, *, descending=False, page_size=256, through_seq=None):
        """Validate every durable event while keeping one bounded page in memory.

        The high-water mark prevents new writes from extending this read.
        This is the original journal, not a compacted or replacement ledger.
        """
        if isinstance(page_size,bool) or not isinstance(page_size,int) or not 1 <= page_size <= 4096:
            raise ValueError('LOCAL_JOURNAL_PAGE_SIZE_INVALID')
        if not isinstance(descending,bool):
            raise ValueError('LOCAL_JOURNAL_DIRECTION_INVALID')
        if through_seq is None:
            through_seq = self.conn.execute(
                'SELECT COALESCE(max(seq),0) FROM execution_local_events WHERE account_id=%s',
                (account_id,),
            ).fetchone()[0]
        if isinstance(through_seq,bool) or not isinstance(through_seq,int) or through_seq < 0:
            raise ValueError('LOCAL_JOURNAL_HIGH_WATER_INVALID')
        cursor = through_seq + 1 if descending else 0
        comparison, direction = ('<','DESC') if descending else ('>','ASC')
        while True:
            rows = self.conn.execute(
                'SELECT seq,event_kind,payload,content_digest,intent_id FROM execution_local_events '
                'WHERE account_id=%s AND seq '+comparison+' %s AND seq<=%s '
                'ORDER BY seq '+direction+' LIMIT %s',
                (account_id,cursor,through_seq,page_size),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                if str(canonical_sha256(row[2])) != row[3]:
                    raise ReservationRejected('LOCAL_EVENT_DIGEST_MISMATCH')
                yield row
            cursor = rows[-1][0]

    def local_events(self, account_id, *, limit=4096):
        rows = self.conn.execute('SELECT seq,event_kind,payload,content_digest FROM execution_local_events WHERE account_id=%s ORDER BY seq LIMIT %s',
            (account_id,limit+1)).fetchall()
        if len(rows) > limit:
            raise ReservationRejected('LOCAL_RESTORE_BOUND_EXCEEDED')
        for row in rows:
            if str(canonical_sha256(row[2])) != row[3]:
                raise ReservationRejected('LOCAL_EVENT_DIGEST_MISMATCH')
        return tuple(rows)


def latest_position_rows(connection,account_id):
    """Load only latest UTC rows, retaining every same-time ambiguity."""
    return connection.execute(
        """WITH latest AS (
            SELECT canonical_symbol,max(as_of) AS as_of FROM execution_positions
            WHERE account_id=%s GROUP BY canonical_symbol)
            SELECT p.canonical_symbol,p.as_of,p.payload FROM execution_positions p
            JOIN latest l ON l.canonical_symbol=p.canonical_symbol AND l.as_of=p.as_of
            WHERE p.account_id=%s ORDER BY p.canonical_symbol,p.content_digest DESC""",
        (account_id,account_id),
    ).fetchall()


def checkpoint_positions(connection,account_id):
    latest={}
    for _,kind,value,digest,_ in ExecutionStore(connection).iter_local_events(account_id,descending=True):
        if str(canonical_sha256(value))!=digest:raise ReservationRejected('LOCAL_EVENT_DIGEST_MISMATCH')
        if kind!='CHECKPOINT':continue
        records=value.get('portfolio')
        if records is None:records=[value.get('position')]
        for position in records:
            if isinstance(position,dict):latest.setdefault(position['canonical_symbol'],position)
    return latest


def active_execution_state(connection, *, account_id, now):
    from quant_execution.risk_config import RiskContextV2
    from quant_execution.contracts import position_from_json
    account=connection.execute("SELECT payload FROM execution_accounts WHERE account_id=%s",(account_id,)).fetchone()
    if account is None:raise ReservationRejected("ACCOUNT_UNAVAILABLE")
    rows=latest_position_rows(connection,account_id)
    latest={};checkpoints=checkpoint_positions(connection,account_id)
    status=account[0]['reconciliation_status'];groups={}
    for symbol,at,p in rows:groups.setdefault(symbol,[]).append((at,p))
    for symbol,values in groups.items():
        stamp=values[0][0];same=[p for at,p in values if at==stamp]
        distinct={canonical_json(p):p for p in same};checkpoint=checkpoints.get(symbol)
        if checkpoint in same:chosen=checkpoint
        else:
            if len(distinct)!=1:status='UNKNOWN'
            chosen=same[0]
        latest[symbol]=position_from_json(canonical_json(chosen))
    positions=tuple(p for p in latest.values() if p.quantity>0)
    if any(p.reconciliation_status!="RECONCILED" or p.as_of>now for p in latest.values()):status="UNKNOWN"
    pending_rows=connection.execute("""SELECT i.payload,s.status,r.margin FROM execution_intents i
        JOIN execution_submission_states s ON s.intent_id=i.intent_id
        JOIN execution_reservations r ON r.intent_id=i.intent_id
        WHERE i.account_id=%s AND r.state='ACTIVE'
            AND s.status IN ('RESERVED','SUBMITTING','UNKNOWN','ACCEPTED','PARTIALLY_FILLED')""",(account_id,)).fetchall()
    pending=tuple(intent_from_json(canonical_json(p)) for p,_,_ in pending_rows)
    if any(s in {'SUBMITTING','UNKNOWN'} for _,s,_ in pending_rows):status="UNKNOWN"
    risk,exposure=connection.execute("SELECT coalesce(sum(risk),0),coalesce(sum(notional),0) FROM execution_reservations WHERE account_id=%s AND state='ACTIVE'",(account_id,)).fetchone()
    unresolved=connection.execute("""SELECT 1 FROM execution_reservations r JOIN execution_submission_states s USING(intent_id)
        JOIN execution_intents i USING(intent_id) WHERE r.account_id=%s AND r.state='ACTIVE'
        AND s.status IN ('FILLED','CANCELLED')""",(account_id,)).fetchone()
    if unresolved and not positions:status="UNKNOWN"
    occupied_symbols={p.canonical_symbol for p in positions}
    pending_margin=sum((margin for p,_,margin in pending_rows if p['canonical_symbol'] not in occupied_symbols),D0)
    flats=[p.as_of for p in latest.values() if p.quantity==0 and p.source_fill_ids]
    return RiskContextV2(positions,pending,max(D0,Decimal(account[0]['available_balance'])-pending_margin),
        risk,status,max(flats) if flats else None,exposure)

D0=Decimal('0')
