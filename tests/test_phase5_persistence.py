from datetime import datetime, timezone
from decimal import Decimal

from quant_phase5.contracts import (
    BreadthState,
    ContextStatus,
    DirectionState,
    MarketLeaderContext,
    MarketRegimeSnapshot,
    RelativeStrengthSnapshot,
    SectorContextSnapshot,
    SectorMembership,
    SectorRelation,
    Stage1Phase5Enrichment,
    StructureState,
    VolatilityState,
    VolumeState,
)
from quant_phase5.persistence import Phase5Repository


NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))


class RecordingConnection:
    def __init__(self):
        self.cursor_value = RecordingCursor()

    def cursor(self):
        return self.cursor_value


def leader():
    return MarketLeaderContext(
        symbol="BTCUSDT",
        timeframe="15m",
        context_timestamp=NOW,
        input_window_start=NOW,
        input_window_end=NOW,
        return_pct=Decimal("0.5"),
        trend_state=DirectionState.TREND_UP,
        structure_state=StructureState.HIGHER_HIGH_HIGHER_LOW,
        volatility_state=VolatilityState.NORMAL,
        volume_state=VolumeState.NORMAL,
        volatility_value=Decimal("0.002"),
        volume_ratio=Decimal("1.1"),
        freshness_status=ContextStatus.AVAILABLE,
        data_quality={"coverage_ratio": "1"},
        source_count=1,
        missing_count=0,
        status=ContextStatus.AVAILABLE,
        processed_at=NOW,
    )


def regime():
    return MarketRegimeSnapshot(
        timeframe="15m",
        context_timestamp=NOW,
        universe_run_id=9,
        direction_regime=DirectionState.TREND_UP,
        volatility_regime=VolatilityState.NORMAL,
        breadth_regime=BreadthState.BROAD_STRENGTH,
        direction_status=ContextStatus.AVAILABLE,
        volatility_status=ContextStatus.AVAILABLE,
        breadth_status=ContextStatus.AVAILABLE,
        status=ContextStatus.AVAILABLE,
        support_evidence=("btc_up",),
        conflict_evidence=(),
        missing_evidence=(),
        processed_at=NOW,
        sample_size=2,
        available_count=2,
        missing_count=0,
        coverage_ratio=Decimal("1"),
    )


def relative_strength():
    return RelativeStrengthSnapshot(
        symbol="BTCUSDT",
        benchmark="ETHUSDT",
        timeframe="15m",
        context_timestamp=NOW,
        candidate_return_pct=Decimal("0.5"),
        benchmark_return_pct=Decimal("0.2"),
        relative_return_pct=Decimal("0.3"),
        relative_class="STRONG",
        universe_run_id=None,
        sample_size=1,
        available_count=1,
        missing_count=0,
        coverage_ratio=Decimal("1"),
        status=ContextStatus.AVAILABLE,
        processed_at=NOW,
    )


def membership():
    return SectorMembership(
        mapping_version="v1",
        symbol="BTCUSDT",
        sector="MAJOR",
        source_reference="config/phase5_sector_map.csv",
        effective_from=NOW,
        effective_to=None,
        status="AVAILABLE",
        processed_at=NOW,
    )


def sector_context():
    return SectorContextSnapshot(
        sector="MAJOR",
        timeframe="15m",
        context_timestamp=NOW,
        universe_run_id=9,
        mapping_version="v1",
        sector_return_pct=Decimal("0.4"),
        sector_positive_ratio=Decimal("0.75"),
        member_count=4,
        sample_size=4,
        available_count=4,
        missing_count=0,
        coverage_ratio=Decimal("1"),
        status=ContextStatus.AVAILABLE,
        processed_at=NOW,
    )


def enrichment():
    return Stage1Phase5Enrichment(
        screening_run_id=7,
        symbol="BTCUSDT",
        universe_run_id=9,
        sector="MAJOR",
        mapping_version="v1",
        candidate_return_pct=Decimal("0.5"),
        sector_return_pct=Decimal("0.4"),
        candidate_vs_sector_pct=Decimal("0.1"),
        sector_relation=SectorRelation.OUTPERFORMING,
        context_status=ContextStatus.AVAILABLE,
        leader_context_ref={"leader": "phase5_market_leader_context"},
        regime_context_ref={"regime": "phase5_market_regime_snapshots"},
        relative_strength_ref={"rs": "phase5_relative_strength_snapshots"},
        sector_context_ref={"sector": "phase5_sector_context_snapshots"},
        processed_at=NOW,
    )

def test_phase5_repository_upserts_all_contracts_without_raw_payload():
    connection = RecordingConnection()
    repository = Phase5Repository(connection)

    assert repository.insert_leader_context([leader()]) == 1
    assert repository.insert_regime_snapshots([regime()]) == 1
    assert repository.insert_relative_strength([relative_strength()]) == 1
    assert repository.insert_sector_membership([membership()]) == 1
    assert repository.insert_sector_context([sector_context()]) == 1
    assert repository.insert_stage1_enrichment([enrichment()]) == 1

    statements = "\n".join(sql for sql, _ in connection.cursor_value.calls).lower()
    for table in (
        "phase5_market_leader_context",
        "phase5_market_regime_snapshots",
        "phase5_relative_strength_snapshots",
        "phase5_sector_membership",
        "phase5_sector_context_snapshots",
        "stage1_phase5_context_enrichment",
    ):
        assert table in statements
    assert "raw_payload" not in statements
    assert statements.count("on conflict") == 6


def test_phase5_repository_uses_replay_identities_and_utc_rows():
    connection = RecordingConnection()
    repository = Phase5Repository(connection)
    repository.insert_leader_context([leader()])
    repository.insert_regime_snapshots([regime()])
    repository.insert_relative_strength([relative_strength()])
    repository.insert_sector_membership([membership()])
    repository.insert_sector_context([sector_context()])
    repository.insert_stage1_enrichment([enrichment()])

    for _, rows in connection.cursor_value.calls:
        for row in rows:
            assert all(
                value.tzinfo is not None and value.utcoffset().total_seconds() == 0
                for value in row
                if isinstance(value, datetime)
            )


def test_phase5_jsonb_persists_decimal_age_without_changing_decimal_text():
    from quant_phase5.persistence import _json

    encoded = _json({
        "freshness_age_seconds": Decimal("2.123"),
        "nested": {"ages": [Decimal("0.001"), Decimal("10.000")]},
    }, "data_quality")

    assert encoded.obj == {
        "freshness_age_seconds": "2.123",
        "nested": {"ages": ["0.001", "10.000"]},
    }


def test_repository_enforces_runtime_evidence_limit_and_canonical_stage1():
    import pytest

    connection = RecordingConnection()
    with pytest.raises(ValueError, match="exceeds"):
        Phase5Repository(connection, max_evidence_bytes=8).insert_leader_context([leader()])
    with pytest.raises(TypeError, match="canonical contract"):
        Phase5Repository(connection).insert_stage1_enrichment([{}])
