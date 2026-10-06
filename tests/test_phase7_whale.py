from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase7.bitcoin import BitcoinBlockParser
from quant_phase7.contracts import DataStatus, MarketKind, ReasonCode, UsdValuation
from quant_phase7.labels import LabelCategory
from quant_phase7.whale import (
    FlowDomain,
    WhaleDirection,
    WhaleThresholdConfig,
    WhaleTier,
    aggregate_whale_window,
    evaluate_whale_transfer,
)


NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)


def _event():
    return BitcoinBlockParser().parse_block({
        "hash": "ab" * 32, "height": 100, "time": 1790035200, "confirmations": 6,
        "tx": [{
            "txid": "01" * 32,
            "vin": [{"txid": "02" * 32, "vout": 0, "prevout": {"scriptPubKey": {"address": "bc1qsource"}}}],
            "vout": [{"n": 0, "value": "0.25000000", "scriptPubKey": {"address": "bc1qdestination"}}],
        }],
    }, observed_at=NOW, fetched_at=NOW, processed_at=NOW)[0]


def _valued(event, amount_usd: Decimal | None = Decimal("12500")):
    valuation = UsdValuation(
        asset_id=event.identity.asset_id, event_time=event.event_time,
        amount_usd=amount_usd, valuation_price=Decimal("50000") if amount_usd is not None else None,
        valuation_exchange="BINANCE" if amount_usd is not None else None,
        valuation_source="fixture" if amount_usd is not None else None,
        valuation_symbol="BTCUSDT", market_kind=MarketKind.SPOT,
        valuation_exchange_timestamp=event.event_time - timedelta(seconds=1) if amount_usd is not None else None,
        valuation_fetched_at=event.event_time if amount_usd is not None else None,
        valuation_skew=timedelta(seconds=1) if amount_usd is not None else None,
        max_valuation_skew=timedelta(seconds=300), status=DataStatus.AVAILABLE if amount_usd is not None else DataStatus.NOT_AVAILABLE,
        reason_code=None if amount_usd is not None else ReasonCode.VALUATION_NOT_AVAILABLE,
    )
    return replace(event, valuation=valuation)


def _config(event, *, version: str = "btc-whale-v1"):
    return WhaleThresholdConfig(
        chain=event.identity.chain, asset_id=event.identity.asset_id,
        threshold_version=version,
        tiers=(WhaleTier("LARGE", Decimal("10000"), None),),
    )


def test_thresholds_are_chain_asset_and_version_specific():
    event = _valued(_event())
    config = _config(event)
    context = evaluate_whale_transfer(event, config, direction=WhaleDirection.INBOUND)
    assert context.tier == "LARGE"
    assert context.threshold_version == "btc-whale-v1"
    assert context.status is DataStatus.AVAILABLE

    with pytest.raises(ValueError, match="at least one tier"):
        WhaleThresholdConfig(
            chain=event.identity.chain, asset_id=event.identity.asset_id,
            threshold_version="bad", tiers=(),
        )
    with pytest.raises(ValueError, match="last tier"):
        WhaleThresholdConfig(
            chain=event.identity.chain, asset_id=event.identity.asset_id,
            threshold_version="bad", tiers=(
                WhaleTier("OPEN", Decimal("10000"), None),
                WhaleTier("HIGHER", Decimal("20000"), None),
            ),
        )


def test_missing_usd_is_not_zero_and_never_becomes_a_whale_negative():
    event = _valued(_event(), None)
    context = evaluate_whale_transfer(event, _config(event), direction=WhaleDirection.OUTBOUND)
    assert context.amount_usd is None
    assert context.tier is None
    assert context.status is DataStatus.PARTIAL
    assert context.reason == "THRESHOLD_NOT_EVALUABLE"
    assert context.large_amount_usd is None


def test_bounded_windows_track_unknown_missing_and_event_time():
    event = _valued(_event())
    inbound = evaluate_whale_transfer(
        event, _config(event), direction=WhaleDirection.INBOUND,
        exchange_involvement=True, label_coverage_ratio=Decimal("1"),
        known_address_count=1, labeled_address_count=1,
    )
    outbound = evaluate_whale_transfer(
        event, _config(event), direction=WhaleDirection.OUTBOUND,
        exchange_involvement=True, label_coverage_ratio=Decimal("1"),
        known_address_count=1, labeled_address_count=1,
    )
    missing = evaluate_whale_transfer(
        _valued(_event(), None), _config(event), direction=WhaleDirection.UNKNOWN,
        label_coverage_ratio=Decimal("1"), known_address_count=1, labeled_address_count=1,
    )
    window_open = event.event_time.replace(second=0, microsecond=0)
    result = aggregate_whale_window(
        [inbound, outbound, missing], chain=event.identity.chain, asset_id=event.identity.asset_id,
        timeframe="5m", window_open=window_open,
        aggregation_version="whale-v1", threshold_version="btc-whale-v1",
        now=window_open,
    )
    assert result.sample_count == 3
    assert result.large_inflow_count == 1
    assert result.large_outflow_count == 1
    assert result.large_inflow_usd == Decimal("12500")
    assert result.large_outflow_usd == Decimal("12500")
    assert result.threshold_not_evaluable_count == 1
    assert result.unknown_transfer_count == 1
    assert result.missing_count == 1
    assert result.status is DataStatus.PARTIAL
    for timeframe in ("1m", "5m", "15m", "1H", "4H"):
        aggregate_whale_window(
            [inbound], chain=event.identity.chain, asset_id=event.identity.asset_id,
            timeframe=timeframe, window_open=window_open,
            aggregation_version="whale-v1", threshold_version="btc-whale-v1",
            now=window_open,
        )


def test_stablecoin_and_bridge_ineligible_context_never_enters_generic_whale_totals():
    event = _valued(_event())
    stablecoin = evaluate_whale_transfer(
        event, _config(event), direction=WhaleDirection.INBOUND,
        flow_domain=FlowDomain.STABLECOIN, aggregation_eligible=False,
    )
    bridge = evaluate_whale_transfer(
        event, _config(event), direction=WhaleDirection.INBOUND,
        flow_domain=FlowDomain.BRIDGE, aggregation_eligible=False,
    )
    result = aggregate_whale_window(
        [stablecoin, bridge], chain=event.identity.chain, asset_id=event.identity.asset_id,
        timeframe="1H", window_open=event.event_time.replace(minute=0, second=0, microsecond=0),
        aggregation_version="whale-v1", threshold_version="btc-whale-v1",
    )
    assert result.sample_count == 0
    assert result.large_inflow_count == 0
    assert result.large_inflow_usd is None
    assert result.status is DataStatus.NOT_AVAILABLE


def test_partial_and_stale_statuses_remain_visible_and_context_has_label_coverage():
    event = _valued(_event())
    partial = evaluate_whale_transfer(
        event, _config(event), direction=WhaleDirection.INBOUND,
        source_category=LabelCategory.KNOWN_EXTERNAL,
        destination_category=LabelCategory.KNOWN_EXCHANGE,
        label_coverage_ratio=Decimal("0.95"),
        known_address_count=20, labeled_address_count=19,
        freshness_status=DataStatus.PARTIAL,
    )
    assert partial.status is DataStatus.PARTIAL
    assert partial.exchange_involvement is True
    assert partial.label_coverage_ratio == Decimal("0.95")

    stale_bridge = evaluate_whale_transfer(
        event, _config(event), direction=WhaleDirection.INBOUND,
        flow_domain=FlowDomain.BRIDGE, aggregation_eligible=False,
        freshness_status=DataStatus.STALE,
    )
    assert stale_bridge.status is DataStatus.STALE

    low_coverage = evaluate_whale_transfer(
        event, _config(event), direction=WhaleDirection.INBOUND,
        label_coverage_ratio=Decimal("0.2"), known_address_count=10, labeled_address_count=2,
    )
    low_result = aggregate_whale_window(
        [low_coverage], chain=event.identity.chain, asset_id=event.identity.asset_id,
        timeframe="1m", window_open=event.event_time.replace(second=0, microsecond=0),
        aggregation_version="whale-v1", threshold_version="btc-whale-v1", now=event.event_time,
    )
    assert low_result.status is DataStatus.NOT_AVAILABLE
    assert low_result.reason == "INSUFFICIENT_LABEL_COVERAGE"

    excluded_result = aggregate_whale_window(
        [stale_bridge], chain=event.identity.chain, asset_id=event.identity.asset_id,
        timeframe="1m", window_open=event.event_time.replace(second=0, microsecond=0),
        aggregation_version="whale-v1", threshold_version="btc-whale-v1", now=event.event_time,
    )
    assert excluded_result.status is DataStatus.STALE
    assert excluded_result.reason == "EXCLUDED_DOMAIN_STALE"

    partial_excluded = replace(stale_bridge, status=DataStatus.PARTIAL)
    partial_result = aggregate_whale_window(
        [partial_excluded], chain=event.identity.chain, asset_id=event.identity.asset_id,
        timeframe="1m", window_open=event.event_time.replace(second=0, microsecond=0),
        aggregation_version="whale-v1", threshold_version="btc-whale-v1", now=event.event_time,
    )
    assert partial_result.status is DataStatus.PARTIAL
    assert partial_result.reason == "EXCLUDED_DOMAIN_PARTIAL"
