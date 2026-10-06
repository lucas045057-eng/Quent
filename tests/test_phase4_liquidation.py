from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal

import pytest

from quant_phase4.adapters.base import AdapterSchemaError
from quant_phase4.adapters.bitget_uta_v3 import BitgetUTA3LiquidationAdapter
from quant_phase4.adapters.bybit_v5 import BybitV5LiquidationAdapter
from quant_phase4.adapters.hyperliquid_public import UnavailableLiquidationAdapter
from quant_phase4.aggregation import LiquidationWindow, LiquidationWindowBuilder, rollup_liquidation_windows
from quant_phase4.contracts import (
    CanonicalLiquidation,
    CoverageSemantics,
    DataStatus,
    LiquidationSide,
    QuantityUnit,
    ReasonCode,
    SourceGranularity,
)
from quant_phase4.liquidation import (
    BoundedLiquidationDeduplicator,
    BoundedLiquidationQueue,
    liquidation_identity_key,
)


BASE = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def bitget_payload(*, side: str = "buy", amount: str = "125", price: str = "100000") -> dict[str, object]:
    return {
        "arg": {"instType": "usdt-futures", "topic": "liquidation", "symbol": "BTCUSDT"},
        "data": [{"symbol": "BTCUSDT", "side": side, "price": price, "amount": amount, "ts": 1789992000000}],
    }


def bybit_payload(*, side: str = "Buy", size: str = "2", price: str = "99900") -> dict[str, object]:
    return {
        "topic": "allLiquidation.BTCUSDT",
        "type": "snapshot",
        "ts": 1789992000100,
        "data": [{"T": 1789992000000, "s": "BTCUSDT", "S": side, "v": size, "p": price}],
    }


def liquidation(
    *,
    event_id: str = "event-1",
    event_timestamp: datetime = BASE,
    side: LiquidationSide = LiquidationSide.LIQUIDATED_LONG,
    price: Decimal = Decimal("100"),
    quantity: Decimal = Decimal("2"),
    unit: QuantityUnit = QuantityUnit.CONTRACTS,
    notional: Decimal | None = None,
    coverage: CoverageSemantics = CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
    granularity: SourceGranularity = SourceGranularity.ALL_LIQUIDATIONS_STREAM,
) -> CanonicalLiquidation:
    return CanonicalLiquidation(
        event_id=event_id,
        exchange="bybit",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        event_timestamp=event_timestamp,
        received_at=BASE,
        processed_at=BASE,
        side=side,
        raw_side="Buy" if side is LiquidationSide.LIQUIDATED_LONG else "Sell",
        raw_side_semantics="EXCHANGE_PROVIDED_LIQUIDATED_POSITION_SIDE",
        price=price,
        raw_quantity=quantity,
        quantity_unit=unit,
        quantity_base=quantity if unit is QuantityUnit.BASE_ASSET else None,
        notional_usd=notional,
        source_endpoint="wss://stream.bybit.com/v5/public/linear",
        source_channel="allLiquidation.BTCUSDT",
        source_granularity=granularity,
        coverage_semantics=coverage,
        status=DataStatus.AVAILABLE,
        raw_reference=None,
        raw_payload=None,
    )


def test_bitget_liquidation_maps_documented_side_and_partial_quote_coin_coverage() -> None:
    rows = BitgetUTA3LiquidationAdapter().parse_ws_message(bitget_payload(), BASE)

    assert len(rows) == 1
    row = rows[0]
    assert row.side is LiquidationSide.LIQUIDATED_LONG
    assert row.raw_side == "buy"
    assert row.raw_quantity == Decimal("125")
    assert row.quantity_unit is QuantityUnit.QUOTE_COIN
    assert row.quantity_base is None
    assert row.notional_usd is None
    assert row.source_granularity is SourceGranularity.AGGREGATED_MAX_PER_SECOND
    assert row.coverage_semantics is CoverageSemantics.PARTIAL_AGGREGATED


def test_bitget_sell_maps_to_liquidated_short() -> None:
    row = BitgetUTA3LiquidationAdapter().parse_ws_message(bitget_payload(side="sell"), BASE)[0]

    assert row.side is LiquidationSide.LIQUIDATED_SHORT
    assert row.raw_side == "sell"


def test_bitget_global_liquidation_payload_uses_data_symbol_when_arg_symbol_is_omitted() -> None:
    payload = bitget_payload()
    payload["arg"] = {"instType": "usdt-futures", "topic": "liquidation"}
    payload["data"][0]["symbol"] = "KERNELUSDT"  # type: ignore[index]

    row = BitgetUTA3LiquidationAdapter().parse_ws_message(payload, BASE)[0]

    assert row.exchange_symbol == "KERNELUSDT"
    assert row.canonical_symbol == "KERNEL-USDT-PERP"
    assert row.source_channel == "liquidation"


def test_bybit_liquidation_preserves_executed_contract_size_and_bankruptcy_price_without_notional() -> None:
    row = BybitV5LiquidationAdapter().parse_ws_message(bybit_payload(), BASE)[0]

    assert row.side is LiquidationSide.LIQUIDATED_LONG
    assert row.raw_side == "Buy"
    assert row.raw_quantity == Decimal("2")
    assert row.quantity_unit is QuantityUnit.CONTRACTS
    assert row.price == Decimal("99900")
    assert row.quantity_base is None
    assert row.notional_usd is None
    assert row.source_granularity is SourceGranularity.ALL_LIQUIDATIONS_STREAM
    assert row.coverage_semantics is CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS


def test_bybit_sell_maps_to_liquidated_short() -> None:
    row = BybitV5LiquidationAdapter().parse_ws_message(bybit_payload(side="Sell"), BASE)[0]

    assert row.side is LiquidationSide.LIQUIDATED_SHORT
    assert row.raw_side == "Sell"


@pytest.mark.parametrize(
    "adapter,payload",
    [
        (BitgetUTA3LiquidationAdapter(), {"arg": {"topic": "ticker"}, "data": []}),
        (BitgetUTA3LiquidationAdapter(), bitget_payload(amount="0")),
        (BitgetUTA3LiquidationAdapter(), bitget_payload(price="NaN")),
        (BybitV5LiquidationAdapter(), {"topic": "allLiquidation.BTCUSDT", "data": []}),
        (BybitV5LiquidationAdapter(), bybit_payload(side="Unknown")),
        (BybitV5LiquidationAdapter(), bybit_payload(size="Infinity")),
    ],
)
def test_liquidation_parsers_reject_malformed_or_semantically_invalid_messages(adapter: object, payload: object) -> None:
    with pytest.raises(AdapterSchemaError):
        adapter.parse_ws_message(payload, BASE)  # type: ignore[attr-defined]


def test_liquidation_parsers_require_utc_received_at_and_integral_epoch_timestamp() -> None:
    with pytest.raises(AdapterSchemaError, match="UTC"):
        BitgetUTA3LiquidationAdapter().parse_ws_message(bitget_payload(), datetime(2026, 9, 21, 12, 0))

    invalid = bitget_payload()
    invalid["data"] = [{"symbol": "BTCUSDT", "side": "buy", "price": "1", "amount": "1", "ts": 1789992000000.5}]
    with pytest.raises(AdapterSchemaError, match="epoch milliseconds"):
        BitgetUTA3LiquidationAdapter().parse_ws_message(invalid, BASE)


@pytest.mark.parametrize("adapter,payload", [
    (BitgetUTA3LiquidationAdapter(), {**bitget_payload(), "arg": {"instType": "usdt-futures", "topic": "liquidation", "symbol": "BTC/USDT"}}),
    (BybitV5LiquidationAdapter(), {**bybit_payload(), "topic": "allLiquidation.BTC/USDT"}),
])
def test_liquidation_parsers_reject_invalid_exchange_symbols(adapter: object, payload: object) -> None:
    if "arg" in payload:
        payload["data"][0]["symbol"] = "BTC/USDT"  # type: ignore[index]
    else:
        payload["data"][0]["s"] = "BTC/USDT"  # type: ignore[index]
    with pytest.raises(AdapterSchemaError, match="symbol"):
        adapter.parse_ws_message(payload, BASE)  # type: ignore[attr-defined]


def test_hyperliquid_liquidation_is_explicitly_unavailable() -> None:
    row = UnavailableLiquidationAdapter().unavailable("BTC", received_at=BASE)

    assert row.status is DataStatus.NOT_AVAILABLE
    assert row.reason_code is ReasonCode.UNCONFIRMED_PUBLIC_SOURCE
    assert row.source_granularity is SourceGranularity.NOT_AVAILABLE
    assert row.coverage_semantics is CoverageSemantics.NOT_AVAILABLE
    assert row.raw_quantity is None
    assert row.notional_usd is None


def test_stable_identity_excludes_received_at_but_distinguishes_side_price_and_quantity() -> None:
    first = liquidation()
    same_event_later = CanonicalLiquidation(**{**first.__dict__, "received_at": BASE + timedelta(seconds=30)})
    opposite = CanonicalLiquidation(**{**first.__dict__, "side": LiquidationSide.LIQUIDATED_SHORT, "raw_side": "Sell"})
    different_price = CanonicalLiquidation(**{**first.__dict__, "price": Decimal("101")})
    different_quantity = CanonicalLiquidation(**{**first.__dict__, "raw_quantity": Decimal("3")})

    assert liquidation_identity_key(first) == liquidation_identity_key(same_event_later)
    assert liquidation_identity_key(first) != liquidation_identity_key(opposite)
    assert liquidation_identity_key(first) != liquidation_identity_key(different_price)
    assert liquidation_identity_key(first) != liquidation_identity_key(different_quantity)


def test_deduplicator_is_bounded_and_expires_entries() -> None:
    dedup = BoundedLiquidationDeduplicator(max_entries_per_exchange=2, ttl_seconds=10)

    assert dedup.add(liquidation(event_id="one"), now=BASE) is True
    assert dedup.add(liquidation(event_id="one"), now=BASE + timedelta(seconds=1)) is False
    assert dedup.add(liquidation(event_id="two"), now=BASE + timedelta(seconds=1)) is True
    assert dedup.add(liquidation(event_id="three"), now=BASE + timedelta(seconds=1)) is True
    assert dedup.size("bybit") == 2
    assert dedup.duplicate_count == 1
    assert dedup.add(liquidation(event_id="one"), now=BASE + timedelta(seconds=11)) is True


def test_queue_has_bounded_backpressure_evidence_and_gap_state() -> None:
    queue = BoundedLiquidationQueue(exchange="bybit", symbol="BTCUSDT", capacity=1)

    assert queue.put_nowait(liquidation(event_id="one"), now=BASE) is True
    assert queue.put_nowait(liquidation(event_id="two"), now=BASE) is False
    assert queue.depth == 1
    assert queue.dropped_count == 1
    assert queue.backpressure_events[0].reason == "LIQUIDATION_BACKPRESSURE"
    assert queue.backpressure_events[0].stream_id == "phase4.liquidation_events"
    assert queue.backpressure_events[0].replay_class.value == "A"
    assert queue.backpressure_events[0].completeness_status.value == "PARTIAL"
    queue.record_disconnect_without_backfill(now=BASE + timedelta(seconds=1))
    assert queue.gap_state.status is DataStatus.STALE
    assert queue.gap_state.reason == "LIQUIDATION_GAP_NO_BACKFILL"
    assert queue.get_nowait().event_id == "one"


def test_minute_windows_aggregate_observed_records_and_preserve_gap_without_fabricating_notional() -> None:
    builder = LiquidationWindowBuilder(max_open_windows=4)
    builder.add(liquidation(event_id="one", notional=None))
    builder.add(liquidation(event_id="two", side=LiquidationSide.LIQUIDATED_SHORT, notional=None))
    builder.mark_gap("bybit", "BTC-USDT-PERP", BASE, reason="LIQUIDATION_GAP_NO_BACKFILL")

    windows = builder.finalize(BASE + timedelta(minutes=2), processed_at=BASE + timedelta(minutes=2))

    assert len(windows) == 1
    window = windows[0]
    assert window.timeframe == "1m"
    assert window.observed_event_count == 2
    assert window.observed_liquidated_long_count == 1
    assert window.observed_liquidated_short_count == 1
    assert window.observed_notional_usd is None
    assert window.status is DataStatus.STALE
    assert window.reason == "LIQUIDATION_GAP_NO_BACKFILL"


def test_minute_windows_sum_verified_notional_only_when_each_source_record_is_verified() -> None:
    builder = LiquidationWindowBuilder(max_open_windows=4)
    builder.add(liquidation(event_id="one", unit=QuantityUnit.BASE_ASSET, notional=Decimal("200")))
    builder.add(liquidation(event_id="two", unit=QuantityUnit.BASE_ASSET, notional=Decimal("300")))

    window = builder.finalize(BASE + timedelta(minutes=2), processed_at=BASE + timedelta(minutes=2))[0]

    assert window.observed_notional_usd == Decimal("500")
    assert window.largest_observed_notional_usd == Decimal("300")


def window(index: int, *, base: datetime, status: DataStatus = DataStatus.AVAILABLE, reason: str | None = None) -> LiquidationWindow:
    opened = base + timedelta(minutes=index)
    return LiquidationWindow(
        exchange="bybit",
        canonical_symbol="BTC-USDT-PERP",
        timeframe="1m",
        window_open=opened,
        window_close=opened + timedelta(minutes=1),
        observed_event_count=1,
        observed_liquidated_long_count=1,
        observed_liquidated_short_count=0,
        observed_notional_usd=Decimal("100"),
        largest_observed_notional_usd=Decimal("100"),
        source_exchange_count=1,
        source_granularity=SourceGranularity.ALL_LIQUIDATIONS_STREAM,
        coverage_semantics=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
        status=status,
        reason=reason,
        processed_at=opened + timedelta(minutes=1),
    )


def test_rollups_are_deterministic_for_all_required_timeframes_and_preserve_partial_coverage() -> None:
    aligned = BASE.replace(hour=0, minute=0)
    for timeframe, minute_count in (("5m", 5), ("15m", 15), ("1H", 60), ("4H", 240)):
        rows = [window(index, base=aligned) for index in range(minute_count)]
        rows[0] = window(0, base=aligned, status=DataStatus.STALE, reason="LIQUIDATION_GAP_NO_BACKFILL")
        rows.append(window(0, base=aligned))

        result = rollup_liquidation_windows(rows, timeframe)

        assert len(result) == 1
        rollup = result[0]
        assert rollup.window_open == aligned
        assert rollup.window_close == aligned + timedelta(minutes=minute_count)
        assert rollup.observed_event_count == minute_count
        assert rollup.observed_notional_usd == Decimal(minute_count * 100)
        assert rollup.status is DataStatus.STALE
        assert rollup.reason == "LIQUIDATION_GAP_NO_BACKFILL"


def test_rollup_marks_missing_minutes_stale_without_inventing_observed_records() -> None:
    result = rollup_liquidation_windows([window(index, base=BASE) for index in (0, 1, 3, 4)], "5m")

    assert result[0].status is DataStatus.STALE
    assert result[0].reason == "MISSING_MINUTE_WINDOWS"
    assert result[0].observed_event_count == 4


def test_rollup_rejects_conflicting_duplicate_minute_windows() -> None:
    first = window(0, base=BASE)
    conflicting = replace(first, observed_event_count=2)

    with pytest.raises(ValueError, match="duplicate"):
        rollup_liquidation_windows([first, conflicting], "5m")


def test_rollup_degrades_mixed_source_semantics_without_combining_observations() -> None:
    partial = replace(
        window(1, base=BASE),
        source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
        coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
    )

    result = rollup_liquidation_windows([window(0, base=BASE), partial], "5m")

    assert len(result) == 1
    degraded = result[0]
    assert degraded.status is DataStatus.ERROR
    assert degraded.reason == "MIXED_SOURCE_SEMANTICS"
    assert degraded.source_granularity is SourceGranularity.NOT_AVAILABLE
    assert degraded.coverage_semantics is CoverageSemantics.NOT_AVAILABLE
    assert degraded.observed_event_count == 0
    assert degraded.observed_notional_usd is None
