from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from quant_phase6.ai import AIRequest, AIResult, AIUsage, StrictSchema
from quant_phase6.persistence import Phase6Repository
from quant_phase6.prompts import PromptDefinition, PromptEnvelope
from quant_phase6.contracts import EventStatus


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


class Cursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, values=None):
        self.calls.append((str(sql), values))

    def executemany(self, sql, values):
        self.calls.append((str(sql), list(values)))


class Connection:
    def __init__(self):
        self.cursor_instance = Cursor()

    def cursor(self):
        return self.cursor_instance


def request():
    envelope = PromptEnvelope("news.classify", "v1", "schema-v1", "policy-v1", "system", "<UNTRUSTED_DATA>{}</UNTRUSTED_DATA>")
    return AIRequest(
        purpose="news_classification", prompt_id="news.classify", prompt_version="v1",
        schema_version="schema-v1", model_policy_version="policy-v1", provider="fake",
        model="fake-model", envelope=envelope, context_hash="a" * 64,
        timeout_seconds=2, max_output_bytes=4096,
    )


def result():
    return AIResult(
        status=EventStatus.AVAILABLE,
        output={"event_type": "SECURITY", "evidence_refs": ["hash"]},
        provider="fake", model="fake-model", request_hash=request().request_hash,
        usage=AIUsage(input_tokens=10, output_tokens=5, total_tokens=15, estimated_cost=Decimal("0.1"), latency_ms=3),
    )


def test_ai_persistence_stores_hashes_versions_and_validated_output_only():
    connection = Connection()
    repo = Phase6Repository(connection)
    analysis_id = repo.insert_ai_analysis(request(), result(), event_kind="news", event_id="evt-1", processed_at=NOW)
    assert analysis_id is None  # fake cursor has no database-generated row; SQL remains executable on PostgreSQL.
    sql, values = connection.cursor_instance.calls[-1]
    assert "insert into phase6_ai_analyses" in sql.lower()
    assert "system" not in repr(values).lower()
    assert "untrusted_data" not in repr(values).lower()
    assert values[0] == "news"
    assert values[2] == request().request_hash


def test_usage_extractions_and_prompt_version_are_bounded():
    connection = Connection()
    repo = Phase6Repository(connection)
    repo.insert_ai_usage(request(), result(), recorded_at=NOW, budget_decision="ALLOW")
    repo.insert_ai_extractions(
        1,
        [{"field_name": "event_type", "field_value": "SECURITY", "evidence_refs": ["hash"], "confidence": Decimal("0.9"), "status": "AVAILABLE", "processed_at": NOW}],
    )
    repo.insert_prompt_version(
        PromptDefinition("news.classify", "v1", "schema-v1", "policy-v1", "system"),
        prompt_hash="b" * 64,
        created_at=NOW,
    )
    assert len(connection.cursor_instance.calls) == 3
    assert all("system" not in repr(call).lower() for call in connection.cursor_instance.calls)
