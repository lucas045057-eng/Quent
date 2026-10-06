"""Isolated, fixture-driven NautilusTrader 1.231.0 execution spike.

The test provider supplies synthetic instruments only. No exchange API or
private credentials are used. This module does not claim a trading strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import nautilus_trader
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy


PINNED_NAUTILUS_VERSION = "1.231.0"
FIXTURE_START_NS = 1_700_000_000_000_000_000


@dataclass(frozen=True, slots=True)
class InstrumentBindingV1:
    core_symbol: str
    canonical_symbol: str
    venue: str
    venue_symbol: str
    instrument_id: str


def bind_fixture_instrument(
    *,
    core_symbol: str,
    canonical_symbol: str,
    venue: str,
    venue_symbol: str,
    instrument: object,
) -> InstrumentBindingV1:
    """Validate an explicit Quant identity against the pinned engine instrument."""
    base = instrument.base_currency.code
    quote = instrument.quote_currency.code
    if not (
        core_symbol == f"{base}{quote}"
        and canonical_symbol == f"{base}-{quote}-PERP"
        and venue_symbol == str(instrument.raw_symbol)
        and venue == instrument.id.venue.value
        and instrument.id.symbol.value == f"{core_symbol}-PERP"
    ):
        raise ValueError("Core/venue/Nautilus instrument identity mismatch")
    return InstrumentBindingV1(
        core_symbol, canonical_symbol, venue, venue_symbol, str(instrument.id),
    )


@dataclass(frozen=True, slots=True)
class FixtureFeatureV1:
    core_symbol: str
    pattern: str
    direction: str
    observed_at_ns: int
    known_at_ns: int
    valid_until_ns: int
    quality: str
    source_ref: str
    oi_usd: Decimal

    def __post_init__(self) -> None:
        if self.pattern not in {
            "TREND_CONTINUATION", "BREAKOUT_CONFIRMATION", "LIQUIDATION_REVERSAL",
        } or self.direction not in {"LONG", "SHORT"}:
            raise ValueError("unsupported fixture pattern or direction")
        if not (0 < self.observed_at_ns <= self.known_at_ns < self.valid_until_ns):
            raise ValueError("feature availability must precede expiry")
        if self.quality != "AVAILABLE" or not self.source_ref:
            raise ValueError("fixture feature is not eligible")
        if not isinstance(self.oi_usd, Decimal) or self.oi_usd <= 0:
            raise ValueError("normalized OI must be known and positive")


@dataclass(frozen=True, slots=True)
class SpikeFillV1:
    core_symbol: str
    direction: str
    pattern: str
    order_id: str
    execution_id: str
    quantity: Decimal
    price: Decimal
    commission_usdt: Decimal
    filled_at_ns: int
    feature_known_at_ns: int
    source_ref: str


@dataclass(frozen=True, slots=True)
class SpikeResultV1:
    nautilus_version: str
    acceptance_kind: str
    fills: tuple[SpikeFillV1, ...]
    positions: dict[str, str]
    total_commission_usdt: Decimal


class _FeatureStrategy(Strategy):
    def __init__(self, instrument: object, binding: InstrumentBindingV1, feature: FixtureFeatureV1):
        super().__init__()
        self.instrument = instrument
        self.binding = binding
        self.feature = feature
        self._submitted = False
        self.fills = []

    def on_start(self) -> None:
        self.subscribe_quote_ticks(self.instrument.id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self._submitted or tick.instrument_id != self.instrument.id:
            return
        if tick.ts_event < self.feature.known_at_ns:
            return
        if tick.ts_event >= self.feature.valid_until_ns:
            return
        side = OrderSide.BUY if self.feature.direction == "LONG" else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=self.instrument.id,
            order_side=side,
            quantity=Quantity.from_str("0.010"),
        )
        self._submitted = True
        self.submit_order(order)

    def on_order_filled(self, event) -> None:
        self.fills.append(event)


def _fixture_quotes(instrument: object, mid: Decimal) -> list[QuoteTick]:
    ticks: list[QuoteTick] = []
    for index in range(4):
        timestamp = FIXTURE_START_NS + index * 1_000_000_000
        ticks.append(QuoteTick(
            instrument.id,
            Price(mid + index - Decimal("1"), instrument.price_precision),
            Price(mid + index + Decimal("1"), instrument.price_precision),
            Quantity(100, instrument.size_precision),
            Quantity(100, instrument.size_precision),
            timestamp, timestamp,
        ))
    return ticks


def run_fixture_spike() -> SpikeResultV1:
    """Replay two typed known-at features through real v1 market orders/fills."""
    if nautilus_trader.__version__ != PINNED_NAUTILUS_VERSION:
        raise RuntimeError("NautilusTrader version does not match pinned v1.231.0")
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    instruments = (
        ("BTCUSDT", "BTC-USDT-PERP", TestInstrumentProvider.btcusdt_perp_binance(),
         "TREND_CONTINUATION", "LONG", Decimal("50000")),
        ("ETHUSDT", "ETH-USDT-PERP", TestInstrumentProvider.ethusdt_perp_binance(),
         "BREAKOUT_CONFIRMATION", "SHORT", Decimal("3000")),
    )
    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")),
    )
    try:
        engine.add_venue(
            venue=Venue("BINANCE"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money.from_str("100000 USDT")],
            base_currency=None,
            default_leverage=Decimal("1"),
            fee_model=MakerTakerFeeModel(),
        )
        strategies: list[_FeatureStrategy] = []
        for core_symbol, canonical, instrument, pattern, direction, mid in instruments:
            binding = bind_fixture_instrument(
                core_symbol=core_symbol, canonical_symbol=canonical, venue="BINANCE",
                venue_symbol=core_symbol, instrument=instrument,
            )
            feature = FixtureFeatureV1(
                core_symbol=core_symbol, pattern=pattern, direction=direction,
                observed_at_ns=FIXTURE_START_NS,
                known_at_ns=FIXTURE_START_NS + 1_000_000_000,
                valid_until_ns=FIXTURE_START_NS + 3_000_000_000,
                quality="AVAILABLE", source_ref=f"fixture:{core_symbol}:phase9",
                oi_usd=Decimal("1000000"),
            )
            engine.add_instrument(instrument)
            engine.add_data(_fixture_quotes(instrument, mid))
            strategy = _FeatureStrategy(instrument, binding, feature)
            engine.add_strategy(strategy)
            strategies.append(strategy)
        engine.run()
        fills: list[SpikeFillV1] = []
        for strategy in strategies:
            for event in strategy.fills:
                if event.commission.currency.code != "USDT":
                    raise ValueError("fixture commission currency mismatch")
                fills.append(SpikeFillV1(
                    core_symbol=strategy.feature.core_symbol,
                    direction=strategy.feature.direction,
                    pattern=strategy.feature.pattern,
                    order_id=str(event.client_order_id),
                    execution_id=str(event.trade_id),
                    quantity=event.last_qty.as_decimal(),
                    price=event.last_px.as_decimal(),
                    commission_usdt=event.commission.as_decimal(),
                    filled_at_ns=event.ts_event,
                    feature_known_at_ns=strategy.feature.known_at_ns,
                    source_ref=strategy.feature.source_ref,
                ))
        positions = {
            next(binding.core_symbol for binding in (strategy.binding for strategy in strategies)
                 if binding.instrument_id == str(position.instrument_id)): position.side.name
            for position in engine.cache.positions()
        }
        return SpikeResultV1(
            nautilus_version=nautilus_trader.__version__,
            acceptance_kind="FIXTURE_DRIVEN_ACCEPTANCE",
            fills=tuple(fills),
            positions=positions,
            total_commission_usdt=sum((fill.commission_usdt for fill in fills), Decimal(0)),
        )
    finally:
        engine.dispose()
