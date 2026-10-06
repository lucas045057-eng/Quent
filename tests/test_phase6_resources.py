from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from quant_phase1.config import Settings
from quant_phase6.ai import TTLCache
from quant_phase6.persistence import cleanup_phase6
from datetime import datetime, timezone


def test_phase6_bounds_fit_existing_runtime_budget():
    settings = Settings.from_env({})
    assert settings.phase6_ai_queue_capacity <= 10_000
    assert settings.phase6_max_raw_bytes <= 8 * 1024 * 1024
    assert settings.phase6_max_ai_input_bytes <= 1_048_576
    assert settings.phase6_max_ai_output_bytes <= 1_048_576
    cache = TTLCache(256, timedelta(hours=settings.phase6_raw_cache_ttl_hours), settings.phase6_max_ai_output_bytes)
    assert cache.max_entries == 256


def test_existing_compose_topology_and_memory_limits_are_unchanged_for_phase6():
    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "mem_limit: 256m" in compose
    assert "mem_limit: 384m" in compose
    assert compose.count("mem_limit:") == 3
    assert compose.count("restart:") == 2


def test_retention_cleanup_uses_configured_phase6_windows():
    settings = Settings.from_env({
        "PHASE6_NEWS_RETENTION_DAYS": "2",
        "PHASE6_MACRO_RETENTION_DAYS": "3",
        "PHASE6_UNLOCK_RETENTION_DAYS": "4",
        "PHASE6_AI_ANALYSIS_RETENTION_DAYS": "5",
        "PHASE6_AI_USAGE_RETENTION_DAYS": "6",
    })

    class Repository:
        def __init__(self):
            self.calls = []

        def cleanup(self, table, cutoff, *, batch_size, max_batches):
            self.calls.append((table, cutoff, batch_size, max_batches))
            return 0

    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    repository = Repository()
    assert cleanup_phase6(repository, settings, now=now) == {
        "phase6_news_events": 0,
        "phase6_macro_events": 0,
        "phase6_unlock_events": 0,
        "phase6_ai_analyses": 0,
        "phase6_ai_extractions": 0,
        "phase6_ai_usage": 0,
    }
    assert repository.calls[0][1].day == 20
    assert repository.calls[-1][1].day == 16
