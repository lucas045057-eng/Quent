from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase7.contracts import Chain, DataStatus
from quant_phase7.labels import (
    AddressLabel,
    ExchangeFlowDirection,
    LabelCategory,
    LabelSnapshot,
    classify_exchange_flow,
)


NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
ETH_EXTERNAL = "0x" + "11" * 20
ETH_EXCHANGE = "0x" + "22" * 20
ETH_PROTOCOL = "0x" + "33" * 20
ETH_UNKNOWN = "0x" + "44" * 20


def _label(address: str, category: LabelCategory, *, source_id: str = "reviewed") -> AddressLabel:
    return AddressLabel(
        chain=Chain.ETHEREUM,
        address=address,
        category=category,
        source_id=source_id,
        source_version="registry-v1",
        label_version="snapshot-2026-09-23",
        confidence=Decimal("1.0"),
        snapshot_hash="a" * 64,
        source_reference="https://labels.example/snapshot-1.json",
        effective_from=NOW - timedelta(hours=1),
        effective_to=NOW + timedelta(hours=1),
        observed_at=NOW - timedelta(minutes=1),
        updated_at=NOW,
        status=DataStatus.AVAILABLE,
        reason=None,
    )


def _snapshot(labels: list[AddressLabel], denominator: int, *, source_id: str = "reviewed") -> LabelSnapshot:
    return LabelSnapshot(
        chain=Chain.ETHEREUM,
        source_id=source_id,
        source_version="registry-v1",
        label_version="snapshot-2026-09-23",
        source_urls=(f"https://labels.example/{source_id}.json",),
        reviewer="operator-1",
        snapshot_hash="a" * 64,
        effective_from=NOW - timedelta(hours=1),
        effective_to=NOW + timedelta(hours=1),
        coverage_denominator=denominator,
        reviewed=True,
        labels=tuple(labels),
    )


def test_label_snapshot_is_versioned_and_effective_time_safe():
    label = _label(ETH_EXCHANGE, LabelCategory.KNOWN_EXCHANGE)
    snapshot = LabelSnapshot(
        chain=Chain.ETHEREUM,
        source_id="reviewed",
        source_version="registry-v1",
        label_version="snapshot-2026-09-23",
        source_urls=("https://labels.example/snapshot-1.json",),
        reviewer="operator-1",
        snapshot_hash="a" * 64,
        effective_from=label.effective_from,
        effective_to=label.effective_to,
        coverage_denominator=10,
        reviewed=True,
        labels=(label,),
    )
    assert snapshot.labels[0].active_at(NOW)
    assert not snapshot.labels[0].active_at(NOW + timedelta(hours=2))
    assert snapshot.to_rows()[0]["label_version"] == "snapshot-2026-09-23"

    with pytest.raises(ValueError, match="reviewer"):
        LabelSnapshot(
            chain=Chain.ETHEREUM, source_id="reviewed", source_version="registry-v1",
            label_version="snapshot-2026-09-23", source_urls=(), reviewer="",
            snapshot_hash="a" * 64, effective_from=label.effective_from,
            effective_to=label.effective_to, coverage_denominator=10, reviewed=True, labels=(label,),
        )
    with pytest.raises(ValueError, match="reviewed"):
        LabelSnapshot(
            chain=Chain.ETHEREUM, source_id="reviewed", source_version="registry-v1",
            label_version="snapshot-2026-09-23", source_urls=("https://labels.example/snapshot-1.json",),
            reviewer="operator-1", snapshot_hash="a" * 64, effective_from=label.effective_from,
            effective_to=label.effective_to, coverage_denominator=10, reviewed=False, labels=(label,),
        )


def test_coverage_thresholds_are_explicit_and_do_not_guess_missing_wallets():
    labels = [_label(ETH_EXTERNAL, LabelCategory.KNOWN_EXTERNAL)]
    below = classify_exchange_flow(
        chain=Chain.ETHEREUM, from_address=ETH_EXTERNAL, to_address=ETH_EXCHANGE,
        observed_addresses={ETH_EXTERNAL, ETH_EXCHANGE, *["0x" + f"{i:040x}" for i in range(8)]},
        snapshot=_snapshot(labels + [_label(ETH_EXCHANGE, LabelCategory.KNOWN_EXCHANGE)], 10),
        event_time=NOW, min_coverage=Decimal("0.90"),
    )
    assert below.label_coverage_ratio == Decimal("0.2")
    assert below.status is DataStatus.NOT_AVAILABLE
    assert below.direction is ExchangeFlowDirection.UNKNOWN
    assert below.aggregation_eligible is False

    at_threshold = classify_exchange_flow(
        chain=Chain.ETHEREUM, from_address=ETH_EXTERNAL, to_address=ETH_EXCHANGE,
        observed_addresses={ETH_EXTERNAL, ETH_EXCHANGE, ETH_UNKNOWN, *["0x" + f"{i:040x}" for i in range(7)]},
        snapshot=_snapshot([
            _label(ETH_EXTERNAL, LabelCategory.KNOWN_EXTERNAL),
            _label(ETH_EXCHANGE, LabelCategory.KNOWN_EXCHANGE),
            _label(ETH_UNKNOWN, LabelCategory.UNKNOWN),
            *[_label("0x" + f"{i:040x}", LabelCategory.UNKNOWN) for i in range(6)],
        ], 10),
        event_time=NOW, min_coverage=Decimal("0.90"),
    )
    assert at_threshold.label_coverage_ratio == Decimal("0.9")
    assert at_threshold.status is DataStatus.PARTIAL
    assert at_threshold.direction is ExchangeFlowDirection.INBOUND


def test_exchange_flow_direction_is_conservative_and_internal_is_excluded():
    labels = [
        _label(ETH_EXTERNAL, LabelCategory.KNOWN_EXTERNAL),
        _label(ETH_EXCHANGE, LabelCategory.KNOWN_EXCHANGE),
        _label(ETH_PROTOCOL, LabelCategory.KNOWN_PROTOCOL),
    ]
    inbound = classify_exchange_flow(
        chain=Chain.ETHEREUM, from_address=ETH_EXTERNAL, to_address=ETH_EXCHANGE,
        observed_addresses={ETH_EXTERNAL, ETH_EXCHANGE}, snapshot=_snapshot(labels, 2), event_time=NOW,
    )
    assert inbound.direction is ExchangeFlowDirection.INBOUND
    assert inbound.status is DataStatus.AVAILABLE
    assert inbound.aggregation_eligible is True

    outbound = classify_exchange_flow(
        chain=Chain.ETHEREUM, from_address=ETH_EXCHANGE, to_address=ETH_EXTERNAL,
        observed_addresses={ETH_EXTERNAL, ETH_EXCHANGE}, snapshot=_snapshot(labels, 2), event_time=NOW,
    )
    assert outbound.direction is ExchangeFlowDirection.OUTBOUND

    internal = classify_exchange_flow(
        chain=Chain.ETHEREUM, from_address=ETH_EXCHANGE, to_address=ETH_EXCHANGE,
        observed_addresses={ETH_EXCHANGE}, snapshot=_snapshot(labels, 1), event_time=NOW,
    )
    assert internal.direction is ExchangeFlowDirection.INTERNAL
    assert internal.aggregation_eligible is False

    excluded = classify_exchange_flow(
        chain=Chain.ETHEREUM, from_address=ETH_EXCHANGE, to_address=ETH_PROTOCOL,
        observed_addresses={ETH_EXCHANGE, ETH_PROTOCOL}, snapshot=_snapshot(labels, 2), event_time=NOW,
    )
    assert excluded.direction is ExchangeFlowDirection.EXCLUDED
    assert excluded.aggregation_eligible is False


def test_unknown_and_conflicting_labels_never_become_external():
    unknown = classify_exchange_flow(
        chain=Chain.ETHEREUM, from_address=ETH_UNKNOWN, to_address=ETH_EXCHANGE,
        observed_addresses={ETH_UNKNOWN, ETH_EXCHANGE},
        snapshot=_snapshot([_label(ETH_UNKNOWN, LabelCategory.UNKNOWN), _label(ETH_EXCHANGE, LabelCategory.KNOWN_EXCHANGE)], 2),
        event_time=NOW,
    )
    assert unknown.direction is ExchangeFlowDirection.UNKNOWN
    assert unknown.status is DataStatus.PARTIAL
    assert unknown.reason == "UNKNOWN_ENDPOINT"

    conflict = classify_exchange_flow(
        chain=Chain.ETHEREUM, from_address=ETH_EXTERNAL, to_address=ETH_EXCHANGE,
        observed_addresses={ETH_EXTERNAL, ETH_EXCHANGE},
        snapshot=(
            _snapshot([_label(ETH_EXTERNAL, LabelCategory.KNOWN_EXTERNAL, source_id="source-a"), _label(ETH_EXCHANGE, LabelCategory.KNOWN_EXCHANGE, source_id="source-a")], 2, source_id="source-a"),
            _snapshot([_label(ETH_EXTERNAL, LabelCategory.KNOWN_EXCHANGE, source_id="source-b"), _label(ETH_EXCHANGE, LabelCategory.KNOWN_EXCHANGE, source_id="source-b")], 2, source_id="source-b"),
        ),
        event_time=NOW,
    )
    assert conflict.direction is ExchangeFlowDirection.UNKNOWN
    assert conflict.status is DataStatus.PARTIAL
    assert conflict.reason == "SOURCE_CONFLICT"
    assert len(conflict.conflict_references) == 2
    assert all('"reviewer":"operator-1"' in reference for reference in conflict.conflict_references)


def test_reviewed_snapshot_and_minimum_coverage_cannot_be_bypassed():
    snapshot = _snapshot([
        _label(ETH_EXTERNAL, LabelCategory.KNOWN_EXTERNAL),
        _label(ETH_EXCHANGE, LabelCategory.KNOWN_EXCHANGE),
    ], 2)
    with pytest.raises(ValueError, match="Snapshot"):
        classify_exchange_flow(
            chain=Chain.ETHEREUM, from_address=ETH_EXTERNAL, to_address=ETH_EXCHANGE,
            observed_addresses={ETH_EXTERNAL, ETH_EXCHANGE}, snapshot=snapshot.labels,
            event_time=NOW,
        )
    with pytest.raises(ValueError, match="0.90"):
        classify_exchange_flow(
            chain=Chain.ETHEREUM, from_address=ETH_EXTERNAL, to_address=ETH_EXCHANGE,
            observed_addresses={ETH_EXTERNAL, ETH_EXCHANGE}, snapshot=snapshot,
            event_time=NOW, min_coverage=Decimal("0.1"),
        )
