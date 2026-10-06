from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import re
from decimal import Decimal
import pytest

from quant_phase1.config import Settings
from quant_phase4.aggregation import LiquidationWindowBuilder
from quant_phase4.cross_exchange import build_phase4_context
from quant_phase4.enrichment import enrich_stage1_phase4
from quant_phase1.contracts import DataStatus as Phase1Status
from quant_phase1.stage1 import Stage1Result
from quant_phase4.contracts import (
    CanonicalLiquidation,
    CoverageSemantics,
    DataStatus,
    LiquidationSide,
    QuantityUnit,
    SourceGranularity,
)
from quant_phase4.liquidation import BoundedLiquidationDeduplicator, BoundedLiquidationQueue
from quant_phase4.persistence import Phase4Retention
from quant_phase4.runtime import Phase4Runtime, _bounded_symbols
from tests._migration_helpers import normalized_sha256


ROOT = Path(__file__).parents[1]
EXPECTED_MIGRATION_HASHES = {
    "001_phase1_core.sql": "1847ede5653c8eaf340604f42963fc1782e4dca4bf42237be449b0fbbc1c6ded",
    "002_runtime_health_and_explainability.sql": "70053152dc7bde1363ae93e5b4d8e919e5335261fcf9b8d456e44f3fb435342d",
    "003_kline_source_exchange.sql": "5740d6ce87b9010b2b9e9ff9be1f876e4b1174950b76fbf6f45a59ce3dc97bab",
    "004_phase2_derivatives.sql": "66dc492a3b342d19a0242e5bc218f5fb515c847a9f18a81cba0375601d2012e6",
    "005_phase2_observation_idempotency.sql": "57109d9554e548af6a4fcd949977a44d54482583f132378b67df2574110d5f37",
    "006_phase2_stage1_enrichment.sql": "b290e9656a403678f6d000530554102a94dcfd63400a9e80690cd37c8291a3f2",
    "007_phase1_runtime_query_indexes.sql": "57937096b8a32f7afc06f983969a715a3aeae28f21b1de020b824cf105d3b39d",
    "008_phase3_flow.sql": "3c1cc3e0b50cc0aa0a8329ab7da8f9f704c9f23dde620a44f949502c4ac430b2",
}


def _source_tree() -> str:
    paths = list((ROOT / "src" / "quant_phase4").rglob("*.py"))
    paths += [ROOT / "src" / "quant_phase1" / "entrypoints" / "collector.py"]
    paths += [ROOT / "src" / "quant_phase1" / "entrypoints" / "engine.py"]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _liquidation(event_id: str, event_timestamp: datetime) -> CanonicalLiquidation:
    return CanonicalLiquidation(
        event_id=event_id,
        exchange="bitget",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        event_timestamp=event_timestamp,
        received_at=event_timestamp,
        processed_at=event_timestamp,
        side=LiquidationSide.LIQUIDATED_LONG,
        raw_side="buy",
        raw_side_semantics="PUBLIC",
        price=Decimal("100"),
        raw_quantity=Decimal("1"),
        quantity_unit=QuantityUnit.BASE_ASSET,
        quantity_base=Decimal("1"),
        notional_usd=Decimal("100"),
        source_endpoint="/public/liquidation",
        source_channel="liquidation",
        source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
        coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
        status=DataStatus.AVAILABLE,
        raw_reference=None,
        raw_payload={"debug": "not persisted"},
    )


def _migration_statements(sql: str) -> tuple[str, ...]:
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return tuple(
        re.sub(r"\s+", " ", statement).strip().lower()
        for statement in without_comments.split(";")
        if statement.strip()
    )


def _is_additive_migration_statement(statement: str) -> bool:
    return bool(
        re.match(
            r"^(?:create table if not exists [a-z0-9_]+|"
            r"create index if not exists [a-z0-9_]+|"
            r"alter table long_short_observations add column if not exists population_semantics text not null default '[a-z_]+'|"
            r"comment on (?:table|column|index) [a-z0-9_ .]+ is .+|"
            r"insert into [a-z0-9_]+)",
            statement,
        )
    )


def test_phase4_is_paper_only_and_has_no_private_or_live_execution_surface():
    settings = Settings.from_env({})
    assert settings.trading_mode == "paper"
    assert settings.phase4_enabled is False

    source = _source_tree().lower()
    assert not re.search(
        r"\b(?:api[_ -](?:key|secret|passphrase)|access[_ -]key|secret[_ -]key|"
        r"private[_ -]client|authenticated[_ -]client|sign(?:ed|ature|ing)[_ -]request)\b",
        source,
    )
    routes = re.findall(r"['\"](/[^'\"]+)['\"]", source)
    assert not any(
        re.search(r"/(?:trade|order|position|withdraw|leverage)(?:[/_-]|$)", route.lower())
        for route in routes
    )
    assert "liveexecutor" not in source
    assert "real order" not in source
    for credential in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE"):
        with pytest.raises(ValueError, match="private API credentials"):
            Settings.from_env({credential: "present"})


def test_deployment_keeps_postgres_and_exactly_two_application_services_and_limits():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    server = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")
    assert set(re.findall(r"^  ([a-z0-9-]+):$", compose, re.MULTILINE)) == {
        "postgres", "quant-collector", "quant-engine"
    }
    assert set(re.findall(r"^  (quant-[a-z0-9-]+):$", server, re.MULTILINE)) == {
        "quant-collector", "quant-engine"
    }
    assert "PostgreSQL: 768 MiB" in (ROOT / "PHASE_4_DESIGN_SPEC.md").read_text(encoding="utf-8")
    assert compose.count("mem_limit: 256m") == 1
    assert compose.count("mem_limit: 384m") == 2
    assert server.count("mem_limit: 256m") == 1
    assert server.count("mem_limit: 384m") == 1


def test_phase4_defaults_and_runtime_buffers_are_bounded():
    settings = Settings.from_env({})
    assert settings.phase4_queue_capacity == 2_000
    assert settings.phase4_rest_cycle_seconds == 60.0
    source = _source_tree().lower()
    assert "asyncio.queue()" not in source
    assert "deque(maxlen=" in source
    assert "max_entries_per_exchange" in source
    assert "raw_liquidations" not in source


def test_phase4_bounded_components_enforce_queue_dedup_window_symbol_cache_and_retention():
    settings = Settings.from_env({"PHASE4_ENABLED": "1", "PHASE4_QUEUE_CAPACITY": "2"})
    now = datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc)
    first = _liquidation("one", now)
    second = _liquidation("two", now + timedelta(minutes=1))
    third = _liquidation("three", now + timedelta(minutes=2))

    dedup = BoundedLiquidationDeduplicator(max_entries_per_exchange=2, ttl_seconds=10)
    assert dedup.add(first, now=now)
    assert not dedup.add(first, now=now + timedelta(seconds=1))
    assert dedup.add(second, now=now + timedelta(seconds=1))
    assert dedup.add(third, now=now + timedelta(seconds=1))
    assert dedup.size("bitget") == 2
    assert dedup.add(first, now=now + timedelta(seconds=11))

    queue = BoundedLiquidationQueue(exchange="bitget", symbol="BTCUSDT", capacity=1)
    assert queue.put_nowait(first, now=now)
    assert not queue.put_nowait(second, now=now)
    assert queue.depth == 1
    assert queue.dropped_count == 1
    assert queue.backpressure_events[0].queue_capacity == 1

    builder = LiquidationWindowBuilder(max_open_windows=2)
    assert builder.add(first)
    assert builder.add(second)
    assert not builder.add(third)
    assert builder.dropped_window_count == 1

    runtime = Phase4Runtime(settings)
    assert _bounded_symbols((" btcusdt ", "BTCUSDT", "ethusdt", "solusdt"), 2) == ("BTCUSDT", "ETHUSDT")
    for index in range(10):
        key = ("bitget", "BTC-USDT-PERP", "1m", now + timedelta(minutes=index))
        runtime._remember_window_key(key, now)
    assert len(runtime._persisted_window_keys) <= settings.phase4_queue_capacity

    retention = Phase4Retention(
        liquidation_event_hours=settings.phase4_liquidation_event_retention_hours,
        liquidation_window_days=settings.phase4_liquidation_retention_days,
        long_short_days=settings.phase4_long_short_retention_days,
        basis_days=settings.phase4_basis_retention_days,
        cross_exchange_days=settings.phase4_cross_exchange_retention_days,
        enrichment_days=settings.phase4_enrichment_retention_days,
    )
    assert retention.liquidation_event_hours <= 168
    assert max(retention.liquidation_window_days.values()) <= 365
    assert max(retention.long_short_days, retention.basis_days, retention.cross_exchange_days, retention.enrichment_days) <= 365


def test_global_phase4_state_budget_covers_both_exchanges_and_compact_builder_state():
    settings = Settings.from_env({"PHASE4_ENABLED": "1", "UNIVERSE_LIMIT": "200"})
    runtime = Phase4Runtime(settings)
    now = datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc)
    first = _liquidation("first", now)
    for exchange in ("bitget", "bybit"):
        for index in range(200):
            symbol = f"COIN{index}-USDT-PERP"
            queue = runtime._queue_for(exchange, symbol, now)
            assert queue is not None
            builder = runtime._builders[(exchange, symbol)]
            for minute in range(3):
                builder.add(replace(
                    first, exchange=exchange, exchange_symbol=f"COIN{index}USDT",
                    canonical_symbol=symbol, event_id=f"{exchange}-{index}-{minute}",
                    event_timestamp=now + timedelta(minutes=minute),
                ))
    runtime._compact_state()
    assert len(runtime._queues) == 400
    assert runtime._builder_window_count() <= runtime._builder_window_budget
    assert runtime._state_count() <= runtime._event_budget + runtime._finalized_window_budget + runtime._rollup_window_budget
    assert runtime._state_bytes() <= runtime._event_bytes_budget
    assert all(not hasattr(aggregate, "first") for builder in runtime._builders.values() for aggregate in builder._aggregates.values())


def test_phase4_persists_no_full_raw_liquidation_payload():
    migration = (ROOT / "migrations" / "009_phase4_metrics.sql").read_text(encoding="utf-8").lower()
    persistence = (ROOT / "src" / "quant_phase4" / "persistence.py").read_text(encoding="utf-8").lower()
    assert "raw_payload" not in migration
    assert "raw_payload" not in persistence
    assert "create table if not exists raw_liquidations" not in migration


def test_migrations_001_through_008_are_unchanged_and_009_is_additive_idempotent():
    migrations = ROOT / "migrations"
    actual = {name: normalized_sha256(migrations / name) for name in EXPECTED_MIGRATION_HASHES}
    assert actual == EXPECTED_MIGRATION_HASHES

    sql = (migrations / "009_phase4_metrics.sql").read_text(encoding="utf-8")
    statements = _migration_statements(sql)
    assert statements
    assert all(_is_additive_migration_statement(statement) for statement in statements)
    for destructive in ("truncate", "drop ", "delete ", "update ", "grant ", "revoke "):
        assert not any(statement.startswith(destructive) for statement in statements)
    assert any(statement.startswith("alter table long_short_observations add column if not exists population_semantics") for statement in statements)
    assert not _is_additive_migration_statement("truncate table liquidation_events")
    assert not _is_additive_migration_statement("delete from liquidation_events")
    sql = sql.lower()
    assert sql.count("create table if not exists") == 6
    assert sql.count("create index if not exists") >= 12


def test_phase4_enrichment_preserves_stage1_candidate_and_classification():
    result = Stage1Result(
        "BTCUSDT", "A", "HIGH_CONFIDENCE", Phase1Status.AVAILABLE, ("price",), {}, "BULLISH"
    )
    context = build_phase4_context([], [], [], datetime.now(timezone.utc))
    enriched = enrich_stage1_phase4(result, context, context.processed_at)
    assert enriched.context_only is True
    assert enriched.phase1_result == result
    assert enriched.classification == result.classification
    assert enriched.phase1_result.category == "A"
    assert not hasattr(enriched, "order")
    assert not hasattr(enriched, "position")


def test_completion_report_declares_runtime_pending():
    report = (ROOT / "PHASE_4_COMPLETION_REPORT.md").read_text(encoding="utf-8")
    assert "PHASE4_CODE_COMPLETE_RUNTIME_PENDING" in report
    assert "Jakarta Runtime Acceptance remains pending" in report
    assert "0e63dcc` is not reachable" in report
    assert "808293bff50aeb02b10e2e0ff6d5fbe60b44b9c6" in report
