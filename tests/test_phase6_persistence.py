from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest

from psycopg.types.json import Jsonb

from quant_phase1.db import apply_migrations
from quant_phase6.normalization import normalize_macro, normalize_news, normalize_unlock
from quant_phase6.persistence import Phase6Repository
from quant_phase6.sources import SourceDefinition, SourceRegistry, SourceType


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


class Cursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def executemany(self, sql, values):
        self.calls.append((str(sql), list(values)))

    def execute(self, sql, values=None):
        self.calls.append((str(sql), values))


class Connection:
    def __init__(self):
        self.cursor_instance = Cursor()

    def cursor(self):
        return self.cursor_instance


def registry():
    return SourceRegistry(
        [
            SourceDefinition(
                source_id="fixture.news",
                source_type=SourceType.RSS,
                base_url="https://news.example.test/feed",
                allowed_hosts=("news.example.test",),
                allowed_paths=("/feed", "/article"),
                parser_version="fixture-v1",
                policy_version="policy-v1",
            ),
            SourceDefinition(
                source_id="fixture.macro",
                source_type=SourceType.PUBLIC_API,
                base_url="https://news.example.test/feed",
                allowed_hosts=("news.example.test",),
                allowed_paths=("/feed",),
                parser_version="fixture-v1",
                policy_version="policy-v1",
            ),
            SourceDefinition(
                source_id="fixture.unlock",
                source_type=SourceType.PROJECT,
                base_url="https://news.example.test/feed",
                allowed_hosts=("news.example.test",),
                allowed_paths=("/feed",),
                parser_version="fixture-v1",
                policy_version="policy-v1",
            ),
        ]
    )


def test_event_persistence_is_idempotent_and_bounded():
    connection = Connection()
    repo = Phase6Repository(connection)
    news = normalize_news(
        registry(),
        {"id": "n-1", "url": "https://news.example.test/article/1", "headline": "h", "event_type": "SECURITY"},
        source_id="fixture.news",
        observed_at=NOW,
        fetched_at=NOW,
    )

    assert repo.upsert_news((news,)) == 1
    sql, values = connection.cursor_instance.calls[-1]
    assert "on conflict (source, event_fingerprint)" in sql.lower()
    assert isinstance(values[0][-1], Jsonb)
    assert values[0][6] is None
    assert values[0][7] == NOW


def test_macro_and_unlock_persistence_do_not_coerce_missing_values_to_zero():
    connection = Connection()
    repo = Phase6Repository(connection)
    macro = normalize_macro(
        registry(),
        {"id": "m-1", "event_type": "CPI", "region": "US", "scheduled_at": "2026-09-23T00:00:00Z"},
        source_id="fixture.macro",
        observed_at=NOW,
        fetched_at=NOW,
    )
    unlock = normalize_unlock(
        registry(),
        {"id": "u-1", "symbol": "ABCUSDT", "asset": "ABC", "event_at": "2026-09-23T00:00:00Z", "amount": "100", "amount_unit": "ABC"},
        source_id="fixture.unlock",
        observed_at=NOW,
        fetched_at=NOW,
    )

    assert repo.upsert_macro((macro,)) == 1
    assert repo.upsert_unlock((unlock,)) == 1
    macro_values = connection.cursor_instance.calls[-2][1][0]
    unlock_values = connection.cursor_instance.calls[-1][1][0]
    assert macro_values[17:20] == (None, None, None)
    assert unlock_values[17] is None


def test_retention_cleanup_accepts_only_known_phase6_tables():
    connection = Connection()
    repo = Phase6Repository(connection)
    assert repo.cleanup("phase6_news_events", NOW, batch_size=10, max_batches=1) == 1
    sql, values = connection.cursor_instance.calls[-1]
    assert "limit %s" in sql.lower()
    assert values == (NOW, 10)
    assert repo.cleanup("phase6_ai_extractions", NOW, batch_size=10, max_batches=1) == 1
    assert repo.cleanup("phase6_ai_usage", NOW, batch_size=10, max_batches=1) == 1
    usage_sql, usage_values = connection.cursor_instance.calls[-1]
    assert "recorded_at < %s" in usage_sql.lower()
    assert "order by recorded_at" in usage_sql.lower()
    assert usage_values == (NOW, 10)


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN is not configured")
def test_phase6_postgres_empty_migration_repeat_insert_duplicate_utc_and_retention():
    import psycopg

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as connection:
        apply_migrations(connection)
        apply_migrations(connection)
        assert connection.execute("SELECT current_database(), current_setting('TIMEZONE')").fetchone()[1] == "UTC"
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = '011_phase6_external_context.sql'"
        ).fetchone()[0] == 1
        repo = Phase6Repository(connection)
        event = normalize_news(
            registry(),
            {"id": "integration-1", "url": "https://news.example.test/article/1", "headline": "h", "event_type": "SECURITY"},
            source_id="fixture.news", observed_at=NOW, fetched_at=NOW,
        )
        assert repo.upsert_news((event,)) == 1
        assert repo.upsert_news((event,)) == 1
        assert connection.execute(
            "SELECT count(*) FROM phase6_news_events WHERE source = 'fixture.news' AND event_id = 'fixture.news:integration-1'"
        ).fetchone()[0] == 1
        assert repo.cleanup("phase6_news_events", NOW - timedelta(days=1), batch_size=10, max_batches=1) == 0
