from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase1.contracts import DataStatus as Phase1Status
from quant_phase1.stage1 import Stage1Result
from quant_phase3.contracts import FlowStatus
from quant_phase3.cross_exchange import build_cross_exchange_flow_snapshot
from quant_phase3.cvd import BybitCVDBuilder
from quant_phase3.enrichment import Stage1FlowEnrichment, enrich_stage1_flow
from quant_phase3.flow import TradeFlowWindow


BASE = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def stage1_result():
    return Stage1Result("BTCUSDT", "A", "HIGH_CONFIDENCE", Phase1Status.AVAILABLE, ("price",), {}, "BULLISH")


def flow_window(*, status=FlowStatus.AVAILABLE, delta=Decimal("2"), timeframe="1m"):
    opened = BASE
    total = Decimal("10")
    return TradeFlowWindow(
        exchange="bybit",
        canonical_symbol="BTC-USDT-PERP",
        timeframe=timeframe,
        window_open=opened,
        window_close=opened + timedelta(minutes=1),
        total_trade_count=1,
        buy_trade_count=1,
        sell_trade_count=0,
        unknown_trade_count=0,
        total_volume_base=total,
        buy_volume_base=total,
        sell_volume_base=Decimal("0"),
        unknown_volume_base=Decimal("0"),
        total_notional_usd=Decimal("1000"),
        average_trade_size=total,
        trade_frequency=Decimal(1) / Decimal(60),
        delta_base=delta,
        delta_ratio=delta / total,
        first_trade_at=opened,
        last_trade_at=opened,
        freshness=status,
        status=status,
        status_reason="TRADE_GAP" if status is FlowStatus.PARTIAL else None,
        processed_at=opened + timedelta(minutes=1),
    )


def test_flow_enrichment_is_context_only_and_exposes_available_metrics():
    result = stage1_result()
    enriched = enrich_stage1_flow(
        result,
        canonical_symbol="BTC-USDT-PERP",
        flow_windows=[flow_window()],
        cvd_points=(),
        cross_exchange_snapshot=None,
        processed_at=BASE + timedelta(minutes=2),
    )

    assert isinstance(enriched, Stage1FlowEnrichment)
    assert enriched.flow_status is FlowStatus.AVAILABLE
    assert enriched.flow_context["delta_1m"] == Decimal("2")
    assert enriched.flow_context["delta_ratio_1m"] == Decimal("0.2")
    assert enriched.classification == result.classification
    assert enriched.phase1_result.category == "A"
    assert enriched.context_only is True


def test_flow_enrichment_marks_missing_partial_and_stale_without_changing_stage1():
    result = stage1_result()
    missing = enrich_stage1_flow(
        result,
        canonical_symbol="BTC-USDT-PERP",
        flow_windows=(),
        cvd_points=(),
        cross_exchange_snapshot=None,
        processed_at=BASE,
    )
    partial = enrich_stage1_flow(
        result,
        canonical_symbol="BTC-USDT-PERP",
        flow_windows=[flow_window(status=FlowStatus.PARTIAL)],
        cvd_points=(),
        cross_exchange_snapshot=None,
        processed_at=BASE,
    )

    assert missing.flow_status is FlowStatus.NOT_AVAILABLE
    assert missing.reason == "FLOW_NOT_AVAILABLE"
    assert partial.flow_status is FlowStatus.PARTIAL
    assert partial.reason == "TRADE_GAP"
    assert missing.phase1_result == partial.phase1_result == result


def test_flow_enrichment_does_not_promote_insufficient_directional_sources_to_signal():
    snapshot = build_cross_exchange_flow_snapshot(
        [flow_window()],
        snapshot_timestamp=BASE + timedelta(minutes=1),
        processed_at=BASE + timedelta(minutes=1),
        min_directional_sources=2,
    )
    enriched = enrich_stage1_flow(
        stage1_result(),
        canonical_symbol="BTC-USDT-PERP",
        flow_windows=[flow_window()],
        cvd_points=(),
        cross_exchange_snapshot=snapshot,
        processed_at=BASE + timedelta(minutes=2),
    )

    assert enriched.flow_context["cross_exchange_directional_status"] == "INSUFFICIENT_DIRECTIONAL_SOURCES"
    assert enriched.phase1_result.category == "A"
    assert enriched.phase1_result.reason == "HIGH_CONFIDENCE"
