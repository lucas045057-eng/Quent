"""Pinned local Sandbox session: Nautilus owns matching, orders and portfolio.

The explicit fixture clock permits deterministic reconstruction of the local
simulator. This session has no exchange data/execution client or network route.
"""
import asyncio
from decimal import Decimal

from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, TestClock, MessageBus
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.risk.engine import RiskEngine

from quant_nautilus.adapter import NautilusIntentAdapter, ns
from quant_nautilus.funding import NautilusFundingDriver


class SandboxSession:
    def __init__(self,instrument,intent,*,starting_balance=Decimal("10000")):
        if intent.mode != 'PAPER':
            raise ValueError('Sandbox only accepts local PAPER intents')
        self._initialize_core(instrument, venue=intent.venue, at=intent.created_at,
            starting_balance=starting_balance, max_leverage=intent.max_leverage)
        self.intent = intent
        self.adapter = NautilusIntentAdapter(instrument,intent,auto_submit=False)
        self.adapter.register(self.trader,self.portfolio,self.bus,self.cache,self.clock)
        self.funding = NautilusFundingDriver(self.client.exchange,account_id=intent.account_id,
            canonical_symbol=intent.canonical_symbol,instrument_id=instrument.id)
        self.adapters[intent.intent_id]=self.adapter
        self.instruments[intent.intent_id]=instrument
        self.funding_drivers[intent.intent_id]=self.funding
        self._start_core()
        self.adapter.start()

    def _initialize_core(self, instrument, *, venue, at, starting_balance, max_leverage):
        self.instrument,self.intent = instrument,None
        self.loop = asyncio.new_event_loop()
        self.clock = TestClock()
        self.clock.set_time(ns(at))
        self.cache = Cache()
        self.cache.add_instrument(instrument)
        self.trader = TraderId('QUANT-001')
        self.bus = MessageBus(self.trader,self.clock)
        self.portfolio = Portfolio(self.bus,self.cache,self.clock)
        self.execution = ExecutionEngine(self.bus,self.cache,self.clock)
        self.risk = RiskEngine(self.portfolio,self.bus,self.cache,self.clock)
        self.data = DataEngine(self.bus,self.cache,self.clock)
        self.client = SandboxExecutionClient(self.loop,self.portfolio,self.bus,self.cache,LiveClock(),
            SandboxExecutionClientConfig(venue=venue,starting_balances=[str(starting_balance)+' USDT'],
                default_leverage=max_leverage,bar_execution=False,trade_execution=False,
                use_random_ids=False,use_reduce_only=True))
        self.execution.register_client(self.client)
        self.starting_balance=starting_balance
        self.adapters={}
        self.instruments={}
        self.funding_drivers={}

    def _start_core(self):
        self.data.start()
        self.execution.start()
        self.risk.start()
        self.client.connect()

    @classmethod
    def preflight_empty_account(cls, instrument, *, starting_balance, now, max_leverage):
        """Exercise the original native core without intent, order or journal.

        Caller must first verify the never-traded flat account in its existing
        ledger. This transient native probe never seeds/reset persistent funds.
        Actual order preflight remains intent-bound and unchanged.
        """
        from nautilus_trader.model.objects import Currency
        if (instrument.id.venue.value != 'BITGET_PAPER'
                or not starting_balance.is_finite() or starting_balance <= 0
                or now.tzinfo is None or now.utcoffset().total_seconds() != 0
                or not Decimal(str(max_leverage)).is_finite() or max_leverage < 1):
            raise ValueError('PAPER_OPERATIONAL_PREFLIGHT_INVALID')
        session=cls.__new__(cls)
        try:
            session._initialize_core(instrument,venue='BITGET_PAPER',at=now,
                starting_balance=starting_balance,max_leverage=max_leverage)
            session._start_core()
            account=session.portfolio.account(instrument.id.venue)
            balance=account.balance_total(Currency.from_str('USDT')) if account else None
            if (not session.client.is_connected or balance is None
                    or balance.as_decimal()!=starting_balance
                    or session.cache.orders() or session.cache.positions_open()):
                raise ValueError('PAPER_NATIVE_OPERATIONAL_PREFLIGHT_FAILED')
            return {'paper_engine':True,'reconciliation':True,'native_orders':0}
        finally:
            session.close()

    def add_intent(self,instrument,intent):
        if intent.intent_id in self.adapters:
            if self.adapters[intent.intent_id].intent!=intent:raise ValueError("INTENT_BINDING_MISMATCH")
            return
        if intent.account_id!=self.intent.account_id or intent.venue!=self.intent.venue or intent.mode!="PAPER":
            raise ValueError("SHARED_ACCOUNT_BINDING_INVALID")
        if self.cache.positions_open(instrument_id=instrument.id):raise ValueError("UNSUPPORTED_EXECUTION_CAPABILITY")
        if self.cache.instrument(instrument.id) is None:self.cache.add_instrument(instrument)
        self.client.exchange.leverages[instrument.id]=intent.max_leverage
        self.portfolio.account(instrument.id.venue).set_leverage(instrument.id,intent.max_leverage)
        adapter=NautilusIntentAdapter(instrument,intent,auto_submit=False,strategy_id="INTENT-"+intent.intent_id.hex)
        adapter.register(self.trader,self.portfolio,self.bus,self.cache,self.clock)
        self.adapters[intent.intent_id]=adapter;self.instruments[intent.intent_id]=instrument
        self.funding_drivers[intent.intent_id]=NautilusFundingDriver(self.client.exchange,
            account_id=intent.account_id,canonical_symbol=intent.canonical_symbol,instrument_id=instrument.id)
        adapter.start()

    def select_intent(self,intent_id):
        self.adapter=self.adapters[intent_id];self.intent=self.adapter.intent
        self.instrument=self.instruments[intent_id];self.funding=self.funding_drivers[intent_id]

    def quote(self,at,mid,size):
        stamp = ns(at)
        if stamp < self.clock.timestamp_ns():
            raise ValueError('local fixture clock cannot move backward')
        self.clock.set_time(stamp)
        tick = QuoteTick(self.instrument.id,self.instrument.make_price(mid-1),self.instrument.make_price(mid+1),
            self.instrument.make_qty(size),self.instrument.make_qty(size),stamp,stamp)
        # Update the native book before any command. No bars/close-price fills.
        self.cache.add_quote_tick(tick)
        self.client.on_data(tick)
        self.adapter.on_quote_tick(tick)

    def submit(self):
        self.adapter.submit(self.intent)

    def fund(self,observation):
        payment = self.funding.settle(observation,now=self.adapter.clock.utc_now())
        if payment.status == 'RESOLVED':
            self.adapter._funding_cash = self.funding.ledger.total_cash
        return payment

    def snapshot(self):
        return self.adapter.position_snapshot(self.intent.canonical_symbol)

    def native_order_states(self):
        return {str(order.client_order_id):order.status.name for order in self.cache.orders()}

    def close(self):
        components = (*getattr(self, 'adapters', {}).values(),
            *(getattr(self, name, None) for name in ('risk', 'execution', 'data')))
        try:
            for component in components:
                if component is not None and component.is_running:
                    component.stop()
            client = getattr(self, 'client', None)
            if client is not None:
                client.disconnect()
        finally:
            loop = getattr(self, 'loop', None)
            if loop is not None and not loop.is_closed():
                loop.close()

    def __enter__(self):
        return self

    def __exit__(self,*exc):
        self.close()
