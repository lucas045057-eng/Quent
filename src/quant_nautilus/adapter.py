"""Pinned v1 order/position translation; Python risk approval precedes this boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import nautilus_trader
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FillModel, LatencyModel, MakerTakerFeeModel
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, OrderStatus, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, Venue
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from quant_execution.contracts import ExecutionIntentV1, ExecutionResultV1, PositionSnapshotV1
from quant_phase9.canonical import canonical_sha256


VERSION = 'quant-nautilus-v1.231.0-1'
ZERO = Decimal('0')


def ns(value: datetime) -> int:
    delta = value - datetime(1970,1,1,tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def at_ns(value: int) -> datetime:
    return datetime(1970,1,1,tzinfo=timezone.utc) + timedelta(microseconds=value // 1000)


class NautilusIntentAdapter(Strategy):
    def __init__(self, instrument, intent: ExecutionIntentV1, *, auto_submit=True,
                 result_sink=None, native_sink=None, strategy_id=None):
        if nautilus_trader.__version__ != '1.231.0':
            raise RuntimeError('pinned Nautilus v1.231.0 required')
        if strategy_id is None:super().__init__()
        else:
            from nautilus_trader.trading.config import StrategyConfig
            super().__init__(StrategyConfig(strategy_id=strategy_id,order_id_tag=intent.intent_id.hex[:12]))
        self.instrument = instrument
        self.intent = intent
        self.auto_submit = auto_submit
        self.result_sink = result_sink
        self.native_sink = native_sink
        self.results: list[ExecutionResultV1] = []
        self._submitted = False
        self._protection = 'PENDING'
        self._last_tick = None
        self._funding_cash = None
        self.entry_order_count = 0
        self.fill_events = []
        self._first_fill_ns = None
        self._exit_reason = None
        self._exit_attempts = 0
        self._last_exit_attempt_ns = None

    def capabilities(self):
        return frozenset({'MARKET_IOC','STOP_MARKET_REDUCE_ONLY','CANCEL','POSITION_SNAPSHOT','LOCAL_RECONCILE'})

    def preflight(self, intent, now):
        if intent.content_digest != self.intent.content_digest:
            raise ValueError('intent binding differs from approved intent')
        if (intent.instrument_id != str(self.instrument.id) or intent.venue != self.instrument.id.venue.value
            or intent.canonical_symbol != f'{self.instrument.base_currency.code}-{self.instrument.quote_currency.code}-PERP'
            or intent.settlement_currency != self.instrument.settlement_currency.code
            or self.instrument.is_inverse):
            raise ValueError('instrument binding mismatch')
        if not intent.created_at <= now < intent.valid_until:
            raise ValueError('intent expired or not yet valid')
        quantity = self.instrument.make_qty(intent.approved_quantity)
        if quantity.as_decimal() != intent.approved_quantity or intent.approved_quantity % self.instrument.size_increment.as_decimal() != 0:
            raise ValueError('adapter cannot resize approved quantity')
        if self.instrument.min_quantity is not None and quantity < self.instrument.min_quantity:
            raise ValueError('quantity below instrument minimum')
        if self.instrument.max_quantity is not None and quantity > self.instrument.max_quantity:
            raise ValueError('quantity exceeds instrument maximum')
        if self.instrument.make_price(intent.stop_price).as_decimal() != intent.stop_price:
            raise ValueError('stop precision mismatch')
        if self._last_tick is not None:
            bid,ask = self._last_tick.bid_price.as_decimal(),self._last_tick.ask_price.as_decimal()
            price = ask if intent.side == 'LONG' else bid
            if abs(price-intent.reference_price) / intent.reference_price * 10000 > intent.max_slippage_bps:
                raise ValueError('current quote exceeds execution slippage bound')
            if (ask-bid) / ((ask+bid)/2) * 10000 > intent.max_spread_bps:
                raise ValueError('current quote exceeds execution spread bound')

    def on_start(self):
        self.subscribe_quote_ticks(self.instrument.id)

    def on_quote_tick(self, tick):
        self._last_tick = tick
        if self.auto_submit and not self._submitted and tick.ts_event >= ns(self.intent.created_at):
            self.submit(self.intent)
        if self.intent.strategy_profile is not None:
            self._manage_position()

    def emergency_exit(self):
        if self.intent.strategy_profile is None:
            raise ValueError('strategy safety exit requires a digest-bound Paper plan')
        self._exit_reason = 'EMERGENCY_SAFETY'
        self._manage_position()

    def _manage_position(self):
        positions = self.cache.positions_open(instrument_id=self.instrument.id,strategy_id=self.id)
        if not positions or self._first_fill_ns is None or self._last_tick is None:
            return
        if len(positions) != 1:
            raise ValueError('unresolved native position')
        position = positions[0]
        quantity = position.quantity.as_decimal()
        if quantity <= 0: return
        now = self.clock.timestamp_ns()
        sign = 1 if self.intent.side == 'LONG' else -1
        price = self._last_tick.bid_price.as_decimal() if sign == 1 else self._last_tick.ask_price.as_decimal()
        stop = self.cache.order(self.stop_id)
        if stop is None or not stop.is_open or not stop.is_reduce_only:
            self._exit_reason = 'EMERGENCY_SAFETY'
        if self._exit_reason is None and sign*(price-self.intent.target_price) >= 0:
            self._exit_reason = 'TARGET'
        if self._exit_reason is None and now-self._first_fill_ns >= self.intent.max_hold_seconds*1_000_000_000:
            self._exit_reason = 'MAX_HOLD'
        if self._exit_reason is None or self._last_exit_attempt_ns == now:
            return
        if any(str(o.client_order_id).startswith(self.intent.client_order_id+'-X') and o.is_open
               for o in self.cache.orders(instrument_id=self.instrument.id)):
            return
        self._exit_attempts += 1
        self._last_exit_attempt_ns = now
        order = self.order_factory.market(self.instrument.id,
            OrderSide.SELL if sign == 1 else OrderSide.BUY,
            self.instrument.make_qty(quantity),time_in_force=TimeInForce.IOC,reduce_only=True,
            client_order_id=ClientOrderId(self.intent.client_order_id+'-X'+str(self._exit_attempts)))
        # Keep the protective stop until native fills prove FLAT. Reduce-only
        # prevents either competing exit from creating a reverse position.
        self.submit_order(order)

    def submit(self, intent):
        self.preflight(intent, at_ns(self.clock.timestamp_ns()))
        if self._submitted or self.cache.order(ClientOrderId(intent.client_order_id)) is not None:
            return
        order = self.order_factory.market(self.instrument.id,
            OrderSide.BUY if intent.side == 'LONG' else OrderSide.SELL,
            self.instrument.make_qty(intent.approved_quantity), time_in_force=TimeInForce.IOC,
            client_order_id=ClientOrderId(intent.client_order_id))
        self._submitted = True
        self.entry_order_count += 1
        self.submit_order(order)

    def cancel(self, intent_id: UUID):
        if intent_id != self.intent.intent_id:
            raise ValueError('unknown intent')
        order = self.cache.order(ClientOrderId(self.intent.client_order_id))
        if order is not None and order.is_open:
            self.cancel_order(order)

    def amend(self, intent_id, **constraints):
        raise NotImplementedError('amend is unsupported by this adapter capability set')

    def events(self):
        return iter(tuple(self.results))

    @property
    def stop_id(self):
        return ClientOrderId(self.intent.client_order_id + '-SL')

    def _native(self, event):
        if self.native_sink is not None:
            self.native_sink(event)

    def _emit(self, event, status):
        order = self.cache.order(ClientOrderId(self.intent.client_order_id))
        quantity = order.filled_qty.as_decimal() if order is not None else ZERO
        fees = sum((fee.as_decimal() for fee in order.commissions()), ZERO) if order is not None else ZERO
        price = Decimal(str(order.avg_px)) if quantity > 0 else None
        payload = {'intent':str(self.intent.intent_id),'type':type(event).__name__,
            'order':str(event.client_order_id),'status':status,'filled':quantity,
            'fees':fees,'at':event.ts_event,'protection':self._protection}
        result = ExecutionResultV1(self.intent.intent_id,str(canonical_sha256(payload)),
            self.intent.client_order_id,
            (str(order.venue_order_id),) if order is not None and order.venue_order_id is not None else (),
            status,self.intent.approved_quantity,quantity,self.intent.approved_quantity-quantity,
            price,fees,self.intent.settlement_currency,self._funding_cash,self._protection,
            at_ns(event.ts_event),at_ns(max(event.ts_event,event.ts_init)),
            type(event).__name__,VERSION)
        self.results.append(result)
        if self.result_sink is not None:
            self.result_sink(result)

    def on_order_accepted(self, event):
        self._native(event)
        if event.client_order_id == self.stop_id:
            self._protection = 'ACTIVE'
            entry = self.cache.order(ClientOrderId(self.intent.client_order_id))
            self._emit(event,'FILLED' if entry.status == OrderStatus.FILLED else 'PARTIALLY_FILLED')
        elif str(event.client_order_id) == self.intent.client_order_id:
            self._emit(event,'ACCEPTED')

    def on_order_filled(self, event):
        self._native(event)
        self.fill_events.append(event)
        if str(event.client_order_id) != self.intent.client_order_id:
            if self.intent.strategy_profile is not None:
                positions = self.cache.positions_open(instrument_id=self.instrument.id,strategy_id=self.id)
                if not positions:
                    stop = self.cache.order(self.stop_id)
                    if stop is not None and stop.is_open: self.cancel_order(stop)
                    self._protection = 'NOT_REQUIRED'
                entry = self.cache.order(ClientOrderId(self.intent.client_order_id))
                status = ('FILLED' if entry.filled_qty.as_decimal() == self.intent.approved_quantity
                          else 'CANCELLED' if entry.status == OrderStatus.CANCELED else 'PARTIALLY_FILLED')
                self._emit(event,status)
            return
        if self._first_fill_ns is None: self._first_fill_ns = event.ts_event
        order = self.cache.order(event.client_order_id)
        status = 'FILLED' if order.status == OrderStatus.FILLED else 'PARTIALLY_FILLED'
        # IOC partial fills can leave exposure; protect exactly the cumulative fill.
        stop = self.cache.order(self.stop_id)
        if stop is None:
            protection = self.order_factory.stop_market(self.instrument.id,
                OrderSide.SELL if self.intent.side == 'LONG' else OrderSide.BUY,
                self.instrument.make_qty(order.filled_qty.as_decimal()),
                self.instrument.make_price(self.intent.stop_price), reduce_only=True,
                client_order_id=self.stop_id)
            self.submit_order(protection)
        elif stop.is_open and stop.quantity != order.filled_qty:
            self.modify_order(stop, quantity=self.instrument.make_qty(order.filled_qty.as_decimal()))
        self._emit(event,status)

    def on_order_canceled(self, event):
        self._native(event)
        if str(event.client_order_id) == self.intent.client_order_id:
            self._emit(event,'CANCELLED')
        elif event.client_order_id == self.stop_id:
            self._protection = 'UNKNOWN'

    def on_order_rejected(self, event):
        self._native(event)
        if event.client_order_id == self.stop_id:
            self._protection = 'FAILED'
        elif str(event.client_order_id) == self.intent.client_order_id:
            self._emit(event,'REJECTED')

    def on_order_denied(self, event):
        self.on_order_rejected(event)

    def on_order_submitted(self, event):
        self._native(event)

    def position_snapshot(self, canonical_symbol):
        if canonical_symbol != self.intent.canonical_symbol or self._last_tick is None:
            raise ValueError('position/mark unavailable')
        positions = self.cache.positions(instrument_id=self.instrument.id,strategy_id=self.id)
        if len(positions) != 1:
            raise ValueError('position unresolved')
        position = positions[0]
        account = self.portfolio.account(self.instrument.id.venue)
        currency = Currency.from_str(self.intent.settlement_currency)
        total,free = account.balance_total(currency),account.balance_free(currency)
        locked = account.balance_locked(currency)
        if total is None or free is None or locked is None:
            raise ValueError('account balance unavailable')
        fees = sum((fee.as_decimal() for fee in position.commissions()),ZERO)
        mark = (self._last_tick.bid_price.as_decimal()+self._last_tick.ask_price.as_decimal())/2
        order_ids = tuple(sorted({str(event.client_order_id) for event in position.events}))
        fill_ids = tuple(str(event.trade_id) for event in position.events)
        stop = self.cache.order(self.stop_id)
        protection = 'NOT_REQUIRED' if position.quantity.as_decimal() == 0 else (
            'ACTIVE' if stop is not None and stop.is_open and stop.is_reduce_only
                and stop.quantity.as_decimal() >= position.quantity.as_decimal() else 'FAILED')
        realized = position.realized_pnl.as_decimal() if position.realized_pnl is not None else ZERO
        unrealized = position.unrealized_pnl(self.instrument.make_price(mark)).as_decimal()
        total_unrealized=ZERO
        for opened in self.cache.positions_open():
            tick=self.cache.quote_tick(opened.instrument_id)
            native=self.cache.instrument(opened.instrument_id)
            if tick is None:raise ValueError("shared account mark unavailable")
            current=(tick.bid_price.as_decimal()+tick.ask_price.as_decimal())/2
            total_unrealized+=opened.unrealized_pnl(native.make_price(current)).as_decimal()
        return PositionSnapshotV1(self.intent.account_id,self.intent.venue,canonical_symbol,
            self.intent.mode,position.side.name,position.quantity.as_decimal(),'BASE',
            Decimal(str(position.avg_px_open)) if position.quantity.as_decimal() else None,
            total.as_decimal()+total_unrealized,free.as_decimal(),locked.as_decimal(),
            mark,unrealized,
            realized+fees,fees,self._funding_cash,protection,order_ids,fill_ids,'RECONCILED',
            at_ns(self.clock.timestamp_ns()),VERSION)

    def reconcile(self):
        return (self.position_snapshot(self.intent.canonical_symbol),)


@dataclass(frozen=True, slots=True)
class AdapterBacktestResult:
    results: tuple[ExecutionResultV1,...]
    position: PositionSnapshotV1
    stop_reduce_only: bool
    entry_order_count: int
    funding_payments: tuple = ()
    unresolved_funding: tuple = ()
    economics: object | None = None


def run_intent_backtest(intent, instrument, *, duplicate_submit=False, quote_size=Decimal('100'), funding=(), close_at_final=False):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level='ERROR')))
    try:
        from quant_nautilus.funding import CanonicalFundingModule
        funding_module = CanonicalFundingModule(observations=funding,intent=intent,instrument=instrument) if funding else None
        engine.add_venue(venue=Venue(intent.venue),oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,starting_balances=[Money.from_str('10000 USDT')],
            default_leverage=intent.max_leverage,fee_model=MakerTakerFeeModel(),
            fill_model=FillModel(prob_fill_on_limit=1.0,prob_slippage=0.0,random_seed=7),
            latency_model=LatencyModel(base_latency_nanos=20_000_000),
            modules=[funding_module] if funding_module is not None else [])
        engine.add_instrument(instrument)
        adapter = NautilusIntentAdapter(instrument,intent)
        engine.add_strategy(adapter)
        mids = {}
        for index in range(4):
            timestamp = ns(intent.created_at)+index*1_000_000_000
            mid = intent.reference_price + (-1 if intent.side == 'LONG' else 1) + index
            if close_at_final and index == 3:
                mid = intent.stop_price + (-1 if intent.side == 'LONG' else 1)
            mids[timestamp] = mid
            engine.add_data([QuoteTick(instrument.id,instrument.make_price(mid-1),
                instrument.make_price(mid+1),instrument.make_qty(quote_size),instrument.make_qty(quote_size),
                timestamp,timestamp)])
        engine.run()
        payments = ()
        unresolved = ()
        if funding_module is not None and funding_module.driver is not None:
            payments = funding_module.driver.ledger.payments
            unresolved = tuple(funding_module.driver.unresolved.values())
            if payments and not unresolved:
                adapter._funding_cash = funding_module.driver.ledger.total_cash
        if duplicate_submit:
            adapter.submit(intent)
        position = adapter.position_snapshot(intent.canonical_symbol)
        economics = None
        if position.side == 'FLAT':
            from quant_execution.costs import cost_report
            entries = [fill for fill in adapter.fill_events if str(fill.client_order_id) == intent.client_order_id]
            exits = [fill for fill in adapter.fill_events if fill.client_order_id == adapter.stop_id]
            sign = 1 if intent.side == 'LONG' else -1
            entry_mid = mids[ns(intent.created_at)]
            gross_mid = sign*(sum((fill.last_qty.as_decimal()*mids[fill.ts_event] for fill in exits),ZERO)
                -sum((fill.last_qty.as_decimal()*entry_mid for fill in entries),ZERO))
            spread = sum((fill.last_qty.as_decimal() for fill in entries+exits),ZERO)
            slippage = gross_mid-position.realized_trade_pnl-spread
            economics = cost_report(gross_mid_pnl=gross_mid,trading_pnl=position.realized_trade_pnl,
                fees=position.fees,funding_cash=position.funding_cash,spread_cost=spread,slippage_cost=slippage)
        return AdapterBacktestResult(tuple(adapter.results),
            position,
            engine.cache.order(adapter.stop_id).is_reduce_only,adapter.entry_order_count,
            payments,unresolved,economics)
    finally:
        engine.dispose()
