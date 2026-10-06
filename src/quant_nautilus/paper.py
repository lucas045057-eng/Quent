"""Bounded, durable LOCAL Sandbox driver, with deterministic process restoration.

No generic matching/order/portfolio/reconciliation engine lives here. A journal
replays local simulator inputs into the pinned native Sandbox and compares native
state with PostgreSQL checkpoints. Never use this bridge for an external venue.
"""
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import argparse
import json
import os
from pathlib import Path
import re
import resource
import signal
import time
from itertools import chain
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from quant_execution.contracts import AccountSnapshotV1
from quant_execution.funding import FundingObservationV1
from quant_execution.persistence import ExecutionStore, FencedOwner, ReservationRejected
from quant_nautilus.sandbox import SandboxSession
from quant_nautilus.adapter import ns
from quant_phase9.canonical import canonical_json, canonical_sha256

LOCAL_RESTORE_PAGE_SIZE = 256


def wall_now():
    return datetime.now(timezone.utc)


def payload(value):
    return json.loads(canonical_json(value))


def funding_from_payload(value):
    if set(value) != {f.name for f in fields(FundingObservationV1)}:
        raise ValueError('funding input schema mismatch')
    data = dict(value)
    for key in ('boundary','observed_at','known_at'):
        data[key] = datetime.fromisoformat(data[key].replace('Z','+00:00'))
    for key in ('rate','mark_price'):
        if data[key] is not None:
            if not isinstance(data[key],str):
                raise ValueError('funding decimal must be an exact string')
            data[key] = D(data[key])
    return FundingObservationV1(**data)


class LocalPaper:
    def __init__(
        self, conn, intent_id, *, owner=None, recover_pending_on_start=True,
        owner_lease_seconds=3, acceptance_kind='FIXTURE_DRIVEN_ACCEPTANCE', instrument=None, verify_only=False,
    ):
        self.conn,self.store = conn,ExecutionStore(conn)
        if not isinstance(verify_only, bool):
            raise ValueError('native restore verification mode must be boolean')
        self.verify_only = verify_only
        if verify_only and recover_pending_on_start:
            raise ValueError('verification cannot recover unresolved submissions')
        self.store.assert_ready()
        self.intent = self.store.intent(intent_id)
        if self.intent.mode != 'PAPER':
            raise ValueError('local Paper requires a PAPER intent')
        self.public_instrument=instrument
        if self.intent.strategy_profile=="QUANT_PAPER_V2" and instrument is None:
            # New V2 intents require public specifications, never a test-kit substitute.
            pass  # A durable CONFIG may provide them on restoration below.
        self.owner = owner or 'paper-'+uuid4().hex
        if not isinstance(owner_lease_seconds,int) or isinstance(owner_lease_seconds,bool) or not 1 <= owner_lease_seconds <= 60:
            raise ValueError('local Paper owner lease must be from 1 through 60 seconds')
        if not acceptance_kind:
            raise ValueError('local Paper acceptance kind is required')
        self.owner_lease_seconds = owner_lease_seconds
        self.acceptance_kind = acceptance_kind
        self.epoch = self.store.acquire_owner(self.intent.account_id,self.owner,now=wall_now(),lease_seconds=owner_lease_seconds)
        self.repair_count = 0
        self.restored = False
        self.session = None
        try:
            self.unknown = (
                self.store.recover_pending(self.intent.account_id)
                if recover_pending_on_start else ()
            )
            count, high_water = conn.execute(
                "SELECT count(*),COALESCE(max(seq),0) FROM execution_local_events WHERE account_id=%s",
                (self.intent.account_id,),
            ).fetchone()
            self.event_count=count
            rows=self.store.iter_local_events(
                self.intent.account_id,page_size=LOCAL_RESTORE_PAGE_SIZE,through_seq=high_water,
            )
            first_row=next(rows,None)
            self.public_instruments={}
            self.acceptance_kinds={}
            target=self.intent
            intents={}
            for raw, in conn.execute("SELECT payload FROM execution_intents WHERE account_id=%s",(target.account_id,)).fetchall():
                from quant_execution.contracts import intent_from_json
                item=intent_from_json(canonical_json(raw));intents[item.intent_id]=item
            by_digest={i.content_digest:i for i in intents.values()}
            first=by_digest.get(first_row[2].get('intent_digest')) if first_row else target
            if first is None:raise ReservationRejected("LOCAL_CONFIG_MISMATCH")
            def resolve_instrument(item,config=None):
                if item.strategy_profile=="QUANT_PAPER_V2":
                    raw=(config or {}).get('instrument')
                    if raw is not None:
                        from nautilus_trader.model.instruments import CryptoPerpetual
                        native=CryptoPerpetual.from_dict(raw)
                    elif item.intent_id==target.intent_id and instrument is not None:native=instrument
                    else:raise ReservationRejected("PUBLIC_INSTRUMENT_METADATA_UNAVAILABLE")
                    if str(native.id)!=item.instrument_id:raise ReservationRejected("INSTRUMENT_BINDING_MISMATCH")
                    self.public_instruments[item.intent_id]=native
                    return native
                if item.core_symbol not in {"BTCUSDT","ETHUSDT"}:raise ReservationRejected("HISTORICAL_INSTRUMENT_UNAVAILABLE")
                return TestInstrumentProvider.btcusdt_perp_binance() if item.core_symbol=='BTCUSDT' else TestInstrumentProvider.ethusdt_perp_binance()
            initial_config=first_row[2] if first_row else {}
            balance=D(str(initial_config.get('starting_balance','10000 USDT')).split()[0]) if first_row else D(conn.execute(
                "SELECT payload->>'equity' FROM execution_accounts WHERE account_id=%s",(target.account_id,)).fetchone()[0])
            self.session=SandboxSession(resolve_instrument(first,initial_config),first,starting_balance=balance)
            submitted=set(); configured=set(); current=first.intent_id
            replayed=0;last_kind=None;renewed_at=time.monotonic()
            for _,kind,data,digest,binding in chain((first_row,) if first_row else (),rows):
                if time.monotonic()-renewed_at >= min(1.0,self.owner_lease_seconds/3):
                    self.renew();renewed_at=time.monotonic()
                replayed+=1;last_kind=kind
                if str(canonical_sha256(data))!=digest:raise ReservationRejected("LOCAL_EVENT_DIGEST_MISMATCH")
                if binding is None:
                    if kind=='CONFIG':
                        old=by_digest.get(data.get('intent_digest'))
                        if old is None:raise ReservationRejected("LOCAL_JOURNAL_BINDING_UNKNOWN")
                        current=old.intent_id
                    binding=current
                if binding not in intents:raise ReservationRejected("LOCAL_JOURNAL_BINDING_UNKNOWN")
                if binding not in self.session.adapters:
                    if kind!='CONFIG':raise ReservationRejected("LOCAL_CONFIG_REQUIRED")
                    self.session.add_intent(resolve_instrument(intents[binding],data),intents[binding])
                self.session.select_intent(binding);self.intent=intents[binding]
                if kind=='CONFIG':
                    configured.add(binding)
                    self.acceptance_kinds[binding]=data.get('acceptance_kind')
                    if data!=self.config():raise ReservationRejected('LOCAL_CONFIG_MISMATCH')
                elif kind=='CHECKPOINT':
                    actual=self.state()
                    if 'portfolio' not in data:actual.pop('portfolio',None)
                    if actual!=data:raise ReservationRejected('LOCAL_RECONCILIATION_MISMATCH')
                else:
                    self._apply(kind,data)
                    if kind=='SUBMIT':submitted.add(binding)
            if replayed!=count:
                raise ReservationRejected('LOCAL_JOURNAL_SNAPSHOT_CHANGED')
            self.restored=bool(first_row)
            if any(i not in submitted for i in self.unknown):raise ReservationRejected('UNKNOWN_WITHOUT_DURABLE_LOCAL_COMMAND')
            self.intent=target
            if target.intent_id not in self.session.adapters:
                self.session.add_intent(resolve_instrument(target),target)
            self.session.select_intent(target.intent_id)
            self.acceptance_kinds.setdefault(target.intent_id,acceptance_kind)
            if self.verify_only:
                # Every native CHECKPOINT was compared during the original
                # replay above. A readiness probe may not add commands or
                # append duplicate checkpoints to the bounded durable journal.
                if first_row is None or last_kind != 'CHECKPOINT' or target.intent_id not in submitted:
                    raise ReservationRejected('PAPER_NATIVE_RESTORE_UNPROVEN')
                return
            if submitted:
                self._persist_native(repair=True)
                if target.intent_id in submitted:self.checkpoint()
            if target.intent_id not in configured:self._journal('CONFIG',self.config())
        except BaseException:
            self.close()
            raise

    def config(self):
        config=dict(schema='QUANT_LOCAL_SANDBOX_JOURNAL_V1',nautilus_version='1.231.0',
            intent_digest=self.intent.content_digest,instrument_id=self.intent.instrument_id,
            starting_balance=str(self.session.starting_balance)+' USDT',fee_schedule='test-provider-fixture',
            native_engine='SandboxExecutionClient',acceptance_kind=self.acceptance_kinds.get(self.intent.intent_id,self.acceptance_kind))
        if self.intent.strategy_profile=="QUANT_PAPER_V2":
            config.update(schema='QUANT_LOCAL_SANDBOX_JOURNAL_V2',
                instrument=self.session.instrument.to_dict(self.session.instrument),fee_schedule='explicit-local-paper-costs')
        return config

    def renew(self):
        now = wall_now()
        with self.conn.transaction():
            self.store.assert_owner(self.intent.account_id,self.owner,self.epoch,now=now)
            row = self.conn.execute('''UPDATE execution_accounts SET lease_expires_at=%s
                WHERE account_id=%s AND owner_id=%s AND owner_epoch=%s RETURNING owner_epoch''',
                (now+timedelta(seconds=self.owner_lease_seconds),self.intent.account_id,self.owner,self.epoch)).fetchone()
            if row is None:
                raise FencedOwner('local Paper owner was fenced')

    def _journal(self,kind,data):
        key = str(canonical_sha256(dict(account=self.intent.account_id,seq=self.event_count,kind=kind,payload=data,intent_id=self.intent.intent_id)))
        self.store.append_local_event(account_id=self.intent.account_id,event_key=key,kind=kind,payload=data,intent_id=self.intent.intent_id)
        self.event_count += 1

    def _apply(self,kind,data):
        if kind=='QUOTE':
            if set(data) == {'at','mid','size'}:
                # Kept only for explicitly labelled fixture-driven acceptance.
                self.session.quote(datetime.fromisoformat(data['at'].replace('Z','+00:00')),D(data['mid']),D(data['size']))
            elif set(data) in (
                {'at','bid','ask','bid_size','ask_size'},
                {'at','processed_at','bid','ask','bid_size','ask_size'},
            ):
                self._apply_exact_quote(data)
            else:
                raise ValueError('quote journal schema mismatch')
        elif kind=='SUBMIT':
            if data != {'intent_id':str(self.intent.intent_id),'client_order_id':self.intent.client_order_id}:
                raise ValueError('submission journal binding mismatch')
            self.session.submit()
        elif kind=='FUNDING':
            self.session.fund(funding_from_payload(data))
        elif kind=='EMERGENCY_EXIT':
            if data != {'intent_id':str(self.intent.intent_id)}:
                raise ValueError('exit journal binding mismatch')
            self.session.adapter.emergency_exit()
        elif kind=='CANCEL':
            if data != {'intent_id':str(self.intent.intent_id)}:
                raise ValueError('cancellation journal binding mismatch')
            self.session.adapter.cancel(self.intent.intent_id)
        else:
            raise ValueError('unsupported local journal operation')


    def _apply_exact_quote(self,data):
        if any(not isinstance(data[name],str) for name in ('at','bid','ask','bid_size','ask_size')):
            raise ValueError('exact quote fields must preserve canonical strings')
        at = datetime.fromisoformat(data['at'].replace('Z','+00:00'))
        processed_at = datetime.fromisoformat(data.get('processed_at', data['at']).replace('Z','+00:00'))
        bid,ask,bid_size,ask_size = (D(data[name]) for name in ('bid','ask','bid_size','ask_size'))
        if bid <= 0 or ask < bid or bid_size <= 0 or ask_size <= 0:
            raise ValueError('exact canonical quote is invalid')
        stamp,init_stamp = ns(at),ns(processed_at)
        if init_stamp < stamp:
            raise ValueError('canonical quote processing time precedes exchange time')
        if init_stamp < self.session.clock.timestamp_ns():
            raise ValueError('local QuoteTick cannot move the native clock backward')
        tick = QuoteTick(
            self.session.instrument.id,
            self.session.instrument.make_price(bid),
            self.session.instrument.make_price(ask),
            self.session.instrument.make_qty(bid_size),
            self.session.instrument.make_qty(ask_size),
            stamp,init_stamp,
        )
        if (tick.bid_price.as_decimal()!=bid or tick.ask_price.as_decimal()!=ask
                or tick.bid_size.as_decimal()!=bid_size or tick.ask_size.as_decimal()!=ask_size):
            raise ValueError('Nautilus precision would change the canonical quote')
        self.session.clock.set_time(init_stamp)
        self.session.cache.add_quote_tick(tick)
        self.session.client.on_data(tick)
        self.session.adapter.on_quote_tick(tick)

    def command(self,kind,data,*,crash_after_submit=False,preauthorized=False):
        if self.verify_only:
            raise ReservationRejected('PAPER_NATIVE_VERIFICATION_COMMAND_FORBIDDEN')
        if kind not in {'QUOTE','SUBMIT','FUNDING','CANCEL','EMERGENCY_EXIT'}:
            raise ValueError('unsupported local command')
        self.renew()
        if kind=='SUBMIT':
            submission_state = self.store.submission_state(self.intent.intent_id)
            if preauthorized:
                if submission_state != 'SUBMITTING':
                    raise ReservationRejected('PREAUTHORIZED_SUBMISSION_STATE_INVALID')
            else:
                if submission_state != 'RESERVED':
                    raise ReservationRejected('QUERY_BEFORE_RETRY: command already submitted')
                if not self.store.begin_submission(self.intent.intent_id,self.owner,self.epoch,now=wall_now()):
                    raise ReservationRejected('SUBMISSION_NOT_RESERVED')
        # Write ahead of every simulator side effect. A crash can replay this
        # local command exactly once into a NEW native simulator, never a venue.
        with self.conn.transaction():
            self.store.assert_owner(self.intent.account_id,self.owner,self.epoch,now=wall_now())
            self._journal(kind,data)
        self._apply(kind,data)
        if crash_after_submit and kind=='SUBMIT':
            os._exit(91)  # Explicit fixture fault injection, after durable command.
        if self.session.adapter.results:
            self._persist_native()
            self.checkpoint()

    def _persist_native(self,*,repair=False):
        target=self.intent
        with self.conn.transaction():
            self.store.assert_owner(target.account_id,self.owner,self.epoch,now=wall_now())
            positions=[];funding=[]
            for iid,adapter in self.session.adapters.items():
                if not adapter.results:continue
                self.session.select_intent(iid)
                expected={x.execution_id:str(canonical_sha256(x)) for x in adapter.results}
                existing=dict(self.conn.execute('SELECT execution_id,content_digest FROM execution_results WHERE intent_id=%s',(iid,)).fetchall())
                if any(expected.get(k)!=v for k,v in existing.items()):raise ReservationRejected('LOCAL_RESULT_RECONCILIATION_MISMATCH')
                for result in adapter.results:
                    if result.execution_id not in existing:
                        self.store.record_result(result);self.repair_count+=int(repair)
                position=self.session.snapshot();self.store.record_position(position);positions.append(position)
                funding.extend(self.session.funding.ledger.payments)
            self.session.select_intent(target.intent_id)
            for payment in funding:
                data=payload(payment)
                self.conn.execute("""INSERT INTO execution_funding_payments(payment_key,account_id,canonical_symbol,
                    boundary,cash,payload,applied) VALUES(%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT DO NOTHING""",
                    (payment.payment_key,payment.account_id,payment.canonical_symbol,payment.boundary,payment.cash,Jsonb(data)))
                if self.conn.execute('SELECT payload,applied FROM execution_funding_payments WHERE payment_key=%s',(payment.payment_key,)).fetchone()!=(data,True):
                    raise ReservationRejected('LOCAL_FUNDING_RECONCILIATION_MISMATCH')
            stored_keys={x[0] for x in self.conn.execute('SELECT payment_key FROM execution_funding_payments WHERE account_id=%s',(target.account_id,)).fetchall()}
            if not stored_keys<={p.payment_key for p in funding}:raise ReservationRejected('LOCAL_FUNDING_RECONCILIATION_MISMATCH')
            if positions:
                risk=self.conn.execute("SELECT coalesce(sum(risk),0) FROM execution_reservations WHERE account_id=%s AND state='ACTIVE'",(target.account_id,)).fetchone()[0]
                pending=self.conn.execute("""SELECT count(*) FROM execution_submission_states s JOIN execution_intents i USING(intent_id)
                    WHERE i.account_id=%s AND s.status IN ('RESERVED','SUBMITTING','UNKNOWN','ACCEPTED','PARTIALLY_FILLED')""",(target.account_id,)).fetchone()[0]
                active={p.canonical_symbol:p for p in positions if p.quantity>0}
                last=positions[-1]
                self.store.record_account(AccountSnapshotV1(target.account_id,target.venue,'PAPER',last.equity,
                    last.available_balance,sum((p.quantity*p.mark_price for p in active.values()),D(0)),risk,
                    pending,wall_now(),'RECONCILED'))
            self.unknown=()

    def state(self):
        target=self.session.intent.intent_id
        portfolio={}
        for iid,adapter in self.session.adapters.items():
            if not adapter.results:continue
            self.session.select_intent(iid);position=payload(self.session.snapshot())
            portfolio[position['canonical_symbol']]=position
        self.session.select_intent(target)
        return dict(portfolio=[portfolio[s] for s in sorted(portfolio)],position=payload(self.session.snapshot()),order_states=self.session.native_order_states(),
            result_digests=sorted(str(canonical_sha256(result)) for result in self.session.adapter.results),
            funding_keys=sorted(p.payment_key for p in self.session.funding.ledger.payments),
            entry_order_count=self.session.adapter.entry_order_count)

    def checkpoint(self):
        with self.conn.transaction():
            self.store.assert_owner(self.intent.account_id,self.owner,self.epoch,now=wall_now())
            self._journal('CHECKPOINT',self.state())

    def health(self):
        state = self.state()
        current_rss = int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        return {**state,'state_digest':str(canonical_sha256(state)),'state':'RECONCILED',
            'pid':os.getpid(),'restored':self.restored,'repair_count':self.repair_count,
            'native_engine':'SandboxExecutionClient','acceptance_kind':self.acceptance_kind,
            'rss_bytes':current_rss,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'engine_cap_bytes':384*1024*1024,'journal_count':self.event_count,
            'owner_epoch':self.epoch,'network_order_routes':0,'as_of':wall_now().isoformat()}

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None
        self.store.release_owner(self.intent.account_id,self.owner,self.epoch)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--schema',required=True)
    parser.add_argument('--intent-id',type=UUID,required=True)
    parser.add_argument('--ready-file',type=Path,required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--fixture-run',action='store_true')
    modes.add_argument('--fixture-restore',action='store_true')
    parser.add_argument('--fixture-crash-after-submit',action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'(paper_restart_|quant_paper_fixture_)[a-z0-9_]{1,80}',args.schema):
        raise ValueError('explicit private local fixture schema required')
    dsn = os.environ.get('QUANT_PAPER_DSN','')
    info = conninfo_to_dict(dsn)
    if info.get('host') not in {'127.0.0.1','localhost','::1'} or info.get('dbname') != 'quant_phase9_test':
        raise ValueError('local Paper requires the isolated loopback acceptance database')
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    with psycopg.connect(dsn,options=f'-c search_path={args.schema}',autocommit=True) as conn:
        runner = LocalPaper(conn,args.intent_id)
        try:
            if args.fixture_run:
                if runner.restored:
                    raise ValueError('existing local journal requires fixture-restore')
                intent = runner.intent
                mid = intent.reference_price + (-1 if intent.side=='LONG' else 1)
                runner.command('QUOTE',dict(at=intent.created_at.isoformat(),mid=str(mid),size='100'))
                runner.command('SUBMIT',dict(intent_id=str(intent.intent_id),client_order_id=intent.client_order_id),
                    crash_after_submit=args.fixture_crash_after_submit)
                boundary = intent.created_at+timedelta(seconds=2)
                runner.command('QUOTE',dict(at=boundary.isoformat(),mid=str(mid),size='100'))
                observation = FundingObservationV1(intent.canonical_symbol,boundary,D('0.0001'),mid,'USDT',
                    boundary,boundary,'SETTLED','AVAILABLE','VALID','EXCHANGE_PUBLIC',
                    'fixture:canonical-funding/1','FIXTURE_DRIVEN_ACCEPTANCE')
                runner.command('FUNDING',payload(observation))
            args.ready_file.parent.mkdir(parents=True,exist_ok=True)
            while not stopped:
                runner.renew()
                report = runner.health()
                temporary = args.ready_file.with_suffix('.tmp')
                temporary.write_text(canonical_json(report)+'\n')
                temporary.replace(args.ready_file)
                time.sleep(.5)
        finally:
            runner.close()


if __name__=='__main__':
    main()
