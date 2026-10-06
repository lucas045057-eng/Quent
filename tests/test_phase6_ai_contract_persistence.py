from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

import pytest

from quant_phase6.ai import AIResult, AIUsage
from quant_phase6.contract_v1 import (
    EventStatus,
    build_news_classification_context,
    make_news_classification_request,
    stable_execution_id,
    validate_news_classification_output,
)
from quant_phase6.normalization import normalize_news
from quant_phase6.persistence import Phase6Repository
from quant_phase6.sources import SourceDefinition, SourceRegistry, SourceType


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _event():
    registry = SourceRegistry([
        SourceDefinition(
            source_id="fixture.news", source_type=SourceType.RSS,
            base_url="https://news.example.test/feed",
            allowed_hosts=("news.example.test",), allowed_paths=("/feed", "/article"),
            parser_version="fixture-v1", policy_version="fixture-policy-v1",
        )
    ])
    return normalize_news(
        registry,
        {
            "id": "item-1", "headline": "Protocol reports a security incident",
            "summary": "The project said it is investigating the incident.",
            "published_at": NOW.isoformat(), "event_type": "UNKNOWN",
            "url": "https://news.example.test/article/1",
        },
        source_id="fixture.news", observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    )


class _Cursor:
    def __init__(self, *, fail_on_usage=False):
        self.calls = []
        self.row = None
        self.fail_on_usage = fail_on_usage

    def execute(self, sql, params=None):
        sql = str(sql)
        self.calls.append((sql, params))
        lowered = sql.lower()
        if "insert into phase6_ai_analyses" in lowered:
            self.row = (41,)
        elif "insert into phase6_ai_extractions" in lowered:
            self.row = (42,)
        elif "insert into phase6_ai_usage" in lowered:
            if self.fail_on_usage:
                raise RuntimeError("simulated transaction failure")
            self.row = (43,)
        else:
            self.row = None

    def fetchone(self):
        row, self.row = self.row, None
        return row


class _Connection:
    def __init__(self, *, fail_on_usage=False):
        self.cursor_instance = _Cursor(fail_on_usage=fail_on_usage)
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return self.cursor_instance

    @contextmanager
    def transaction(self):
        try:
            yield self
        except Exception:
            self.rolled_back += 1
            raise
        else:
            self.committed += 1


def _persist(connection):
    prepared = build_news_classification_context(_event(), now=NOW)
    evidence_id = prepared.input_payload["evidence"][0]["evidence_id"]
    request = make_news_classification_request(prepared, provider="fake", model="test-only-model")
    output = {"event_type": "SECURITY", "evidence_ids": [evidence_id]}
    validated = validate_news_classification_output(output, prepared)
    result = AIResult(
        status=EventStatus.AVAILABLE,
        output=output,
        provider="fake",
        model="test-only-model",
        usage=AIUsage(input_tokens=9, output_tokens=3, total_tokens=12, latency_ms=2),
        request_hash=request.request_hash,
    )
    analysis_id = Phase6Repository(connection).persist_news_classification_execution(
        request, result,
        event_id_hash=prepared.event_id_hash,
        validated=validated,
        execution_id=stable_execution_id(prepared.event_id_hash, request.request_hash),
        task_status=EventStatus.AVAILABLE,
        recorded_at=NOW,
        budget_decision="ALLOW",
    )
    return analysis_id, request


def test_analysis_extraction_usage_share_one_atomic_transaction_and_safe_payload():
    connection = _Connection()
    analysis_id, request = _persist(connection)
    assert analysis_id == 41
    assert connection.committed == 1 and connection.rolled_back == 0
    calls = connection.cursor_instance.calls
    assert len(calls) == 3
    assert "on conflict (request_hash) do nothing" in calls[0][0].lower()
    assert "ai_event_type_candidate" in calls[1][0]
    assert "analysis_id, execution_id" in calls[2][0]
    all_values = repr([values for _, values in calls])
    assert "item-1" not in all_values
    assert "news.example.test" not in all_values
    assert "Protocol reports a security incident" not in all_values
    assert request.request_hash in all_values
    assert isinstance(calls[2][1][1], UUID)


def test_usage_failure_rolls_back_the_entire_classification_transaction():
    connection = _Connection(fail_on_usage=True)
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        _persist(connection)
    assert connection.committed == 0
    assert connection.rolled_back == 1
    assert len(connection.cursor_instance.calls) == 3


def test_execution_id_is_deterministic_and_request_scoped():
    event_hash = "a" * 64
    request_a = "b" * 64
    request_b = "c" * 64
    assert stable_execution_id(event_hash, request_a) == stable_execution_id(event_hash, request_a)
    assert stable_execution_id(event_hash, request_a) != stable_execution_id(event_hash, request_b)
