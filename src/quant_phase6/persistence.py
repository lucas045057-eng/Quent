"""Bounded, idempotent PostgreSQL persistence for Phase 6 events."""

from __future__ import annotations

from datetime import datetime, timezone
from datetime import timedelta
import json
from typing import Any, Iterable
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from .ai import AIRequest, AIResult
from .contracts import EventStatus, Importance, MacroEvent, NewsEvent, Provenance, RawReference, UnlockEvent
from .contract_v1 import ValidatedNewsClassification
from .normalization import event_fingerprint
from .prompts import PromptDefinition
from .sources import SourceDefinition, SourceType


MAX_REFERENCE_BYTES = 65_536
_RETENTION_TABLES = {
    "phase6_news_events",
    "phase6_macro_events",
    "phase6_unlock_events",
    "phase6_ai_analyses",
    "phase6_ai_extractions",
    "phase6_ai_usage",
}
_RETRYABLE_AI_ERROR_CODES = {
    "TIMEOUT", "RATE_LIMIT", "AUTHENTICATION", "TRANSPORT", "PROVIDER_REJECTED",
    "BUDGET", "QUEUE_FULL",
}


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _json(value: Any, field: str, *, max_bytes: int = MAX_REFERENCE_BYTES) -> Jsonb:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"{field} exceeds bounded size")
    return Jsonb(value)


def _reference(value: Any, field: str) -> Jsonb | None:
    return None if value is None else _json(value, field)


def _provenance(event: Any) -> dict[str, Any]:
    value = event.provenance
    return {
        "source_id": value.source_id,
        "source_ref": value.source_ref,
        "url": value.url,
        "content_hash": value.content_hash,
        "observed_at": value.observed_at.isoformat(),
        "fetched_at": value.fetched_at.isoformat(),
        "processed_at": value.processed_at.isoformat(),
        "parser_version": value.parser_version,
        "schema_version": value.schema_version,
        "normalization_version": value.normalization_version,
        "model_id": value.model_id,
        "prompt_version": value.prompt_version,
        "extraction_version": value.extraction_version,
    }


def _raw_reference(event: Any) -> dict[str, Any] | None:
    value = event.raw_reference
    if value is None:
        return None
    return {
        "reference": value.reference,
        "content_hash": value.content_hash,
        "byte_size": value.byte_size,
        "expires_at": value.expires_at.isoformat() if value.expires_at else None,
    }


class Phase6Repository:
    """Persist only normalized contracts and bounded provenance references."""

    def __init__(self, connection: Any, *, max_reference_bytes: int = MAX_REFERENCE_BYTES) -> None:
        if max_reference_bytes <= 0:
            raise ValueError("max_reference_bytes must be positive")
        self.connection = connection
        self.max_reference_bytes = max_reference_bytes

    def _json(self, value: Any, field: str) -> Jsonb:
        return _json(value, field, max_bytes=self.max_reference_bytes)

    def upsert_news(self, rows: Iterable[NewsEvent]) -> int:
        values = [
            (
                row.event_id, event_fingerprint(row), row.source, row.source_type.value,
                row.source_ref, row.url, row.published_at, _utc(row.observed_at, "observed_at"),
                row.event_at, _utc(row.fetched_at, "fetched_at"), _utc(row.processed_at, "processed_at"),
                row.event_type, self._json(row.entities, "entities"), self._json(row.symbols, "symbols"),
                row.headline, row.summary, row.importance.value, row.sentiment, row.impact_horizon,
                row.status.value, row.reason_code, row.confidence, row.content_hash, row.parser_version,
                self._json(_raw_reference(row), "raw_reference") if row.raw_reference else None,
                self._json(_provenance(row), "provenance"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase6_news_events (
                event_id, event_fingerprint, source, source_type, source_ref, url,
                published_at, observed_at, event_at, fetched_at, processed_at,
                event_type, entities, symbols, headline, summary, importance,
                sentiment, impact_horizon, status, reason_code, confidence,
                content_hash, parser_version, raw_reference, provenance
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source, event_fingerprint) DO UPDATE SET
                event_id=EXCLUDED.event_id, source_ref=EXCLUDED.source_ref,
                url=EXCLUDED.url, published_at=EXCLUDED.published_at,
                observed_at=EXCLUDED.observed_at, event_at=EXCLUDED.event_at,
                fetched_at=EXCLUDED.fetched_at, processed_at=EXCLUDED.processed_at,
                event_type=EXCLUDED.event_type, entities=EXCLUDED.entities,
                symbols=EXCLUDED.symbols, headline=EXCLUDED.headline,
                summary=EXCLUDED.summary, importance=EXCLUDED.importance,
                sentiment=EXCLUDED.sentiment, impact_horizon=EXCLUDED.impact_horizon,
                status=EXCLUDED.status, reason_code=EXCLUDED.reason_code,
                confidence=EXCLUDED.confidence, content_hash=EXCLUDED.content_hash,
                parser_version=EXCLUDED.parser_version, raw_reference=EXCLUDED.raw_reference,
                provenance=EXCLUDED.provenance
            """,
            values,
        )
        return len(values)

    def upsert_macro(self, rows: Iterable[MacroEvent]) -> int:
        values = [
            (
                row.event_id, event_fingerprint(row), row.source, row.source_type.value,
                row.source_ref, row.url, row.published_at, _utc(row.observed_at, "observed_at"),
                row.event_at, row.scheduled_at, row.released_at, _utc(row.fetched_at, "fetched_at"),
                _utc(row.processed_at, "processed_at"), row.event_type, row.macro_event_type,
                row.region, row.actual, row.forecast, row.previous, row.unit, row.unit,
                row.surprise, self._json(row.entities, "entities"), self._json(row.symbols, "symbols"),
                row.summary, row.importance.value, row.status.value, row.reason_code, row.confidence,
                row.content_hash, row.parser_version,
                self._json(_raw_reference(row), "raw_reference") if row.raw_reference else None,
                self._json(_provenance(row), "provenance"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase6_macro_events (
                event_id, event_fingerprint, source, source_type, source_ref, url,
                published_at, observed_at, event_at, scheduled_at, released_at,
                fetched_at, processed_at, event_type, macro_event_type, region,
                actual, forecast, previous, unit, forecast_unit, surprise, entities,
                symbols, summary, importance, status, reason_code, confidence,
                content_hash, parser_version, raw_reference, provenance
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source, event_fingerprint) DO UPDATE SET
                event_id=EXCLUDED.event_id, source_ref=EXCLUDED.source_ref,
                url=EXCLUDED.url, published_at=EXCLUDED.published_at,
                observed_at=EXCLUDED.observed_at, event_at=EXCLUDED.event_at,
                scheduled_at=EXCLUDED.scheduled_at, released_at=EXCLUDED.released_at,
                fetched_at=EXCLUDED.fetched_at, processed_at=EXCLUDED.processed_at,
                actual=EXCLUDED.actual, forecast=EXCLUDED.forecast, previous=EXCLUDED.previous,
                unit=EXCLUDED.unit, forecast_unit=EXCLUDED.forecast_unit,
                surprise=EXCLUDED.surprise, status=EXCLUDED.status,
                reason_code=EXCLUDED.reason_code, confidence=EXCLUDED.confidence,
                content_hash=EXCLUDED.content_hash, parser_version=EXCLUDED.parser_version,
                raw_reference=EXCLUDED.raw_reference, provenance=EXCLUDED.provenance
            """,
            values,
        )
        return len(values)

    def upsert_unlock(self, rows: Iterable[UnlockEvent]) -> int:
        values = [
            (
                row.event_id, event_fingerprint(row), row.source, row.source_type.value,
                row.source_ref, row.url, row.published_at, _utc(row.observed_at, "observed_at"),
                row.event_at, _utc(row.fetched_at, "fetched_at"), _utc(row.processed_at, "processed_at"),
                row.event_type, row.symbol, row.asset, row.amount, row.amount_unit,
                row.value, row.value_currency, row.value_at, row.circulating_supply,
                row.circulating_supply_unit, row.circulating_supply_ref, row.unlock_pct,
                row.recipient_category.value, self._json(row.entities, "entities"), row.summary,
                row.importance.value, row.status.value, row.reason_code, row.confidence,
                row.content_hash, row.parser_version,
                self._json(_raw_reference(row), "raw_reference") if row.raw_reference else None,
                self._json(_provenance(row), "provenance"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase6_unlock_events (
                event_id, event_fingerprint, source, source_type, source_ref, url,
                published_at, observed_at, event_at, fetched_at, processed_at,
                event_type, symbol, asset, amount, amount_unit, value, value_currency,
                value_at, circulating_supply, circulating_supply_unit,
                circulating_supply_ref, unlock_pct, recipient_category, entities,
                summary, importance, status, reason_code, confidence, content_hash,
                parser_version, raw_reference, provenance
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source, event_fingerprint) DO UPDATE SET
                event_id=EXCLUDED.event_id, source_ref=EXCLUDED.source_ref,
                url=EXCLUDED.url, published_at=EXCLUDED.published_at,
                observed_at=EXCLUDED.observed_at, event_at=EXCLUDED.event_at,
                fetched_at=EXCLUDED.fetched_at, processed_at=EXCLUDED.processed_at,
                symbol=EXCLUDED.symbol, asset=EXCLUDED.asset, amount=EXCLUDED.amount,
                amount_unit=EXCLUDED.amount_unit, value=EXCLUDED.value,
                value_currency=EXCLUDED.value_currency, value_at=EXCLUDED.value_at,
                circulating_supply=EXCLUDED.circulating_supply,
                circulating_supply_unit=EXCLUDED.circulating_supply_unit,
                circulating_supply_ref=EXCLUDED.circulating_supply_ref,
                unlock_pct=EXCLUDED.unlock_pct, recipient_category=EXCLUDED.recipient_category,
                entities=EXCLUDED.entities, summary=EXCLUDED.summary,
                importance=EXCLUDED.importance, status=EXCLUDED.status,
                reason_code=EXCLUDED.reason_code, confidence=EXCLUDED.confidence,
                content_hash=EXCLUDED.content_hash, parser_version=EXCLUDED.parser_version,
                raw_reference=EXCLUDED.raw_reference, provenance=EXCLUDED.provenance
            """,
            values,
        )
        return len(values)

    def upsert_source_registry(self, rows: Iterable[SourceDefinition], *, status: str, processed_at: datetime) -> int:
        values = [
            (
                row.source_id, row.source_type.value, row.base_url, self._json(row.allowed_hosts, "allowed_hosts"),
                self._json(row.allowed_paths, "allowed_paths"), row.parser_version, row.policy_version,
                row.fetch_policy.max_bytes, row.fetch_policy.timeout_seconds, row.fetch_policy.max_redirects,
                status, _utc(processed_at, "processed_at"), self._json({"source_id": row.source_id}, "provenance"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase6_source_registry (
                source_id, source_type, base_url, allowed_hosts, allowed_paths,
                parser_version, policy_version, max_bytes, timeout_seconds,
                max_redirects, status, processed_at, provenance
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source_id) DO UPDATE SET
                source_type=EXCLUDED.source_type, base_url=EXCLUDED.base_url,
                allowed_hosts=EXCLUDED.allowed_hosts, allowed_paths=EXCLUDED.allowed_paths,
                parser_version=EXCLUDED.parser_version, policy_version=EXCLUDED.policy_version,
                max_bytes=EXCLUDED.max_bytes, timeout_seconds=EXCLUDED.timeout_seconds,
                max_redirects=EXCLUDED.max_redirects, status=EXCLUDED.status,
                processed_at=EXCLUDED.processed_at, provenance=EXCLUDED.provenance
            """,
            values,
        )
        return len(values)

    def load_news_classification_candidates(
        self, *, limit: int = 100, offset: int = 0,
        source_ids: Iterable[str] | None = None,
    ) -> tuple[NewsEvent, ...]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("candidate limit must be between 1 and 10000")
        if offset < 0:
            raise ValueError("candidate offset must not be negative")
        from .contract_v1 import NEWS_EVENT_TYPES

        selected_sources = tuple(source_ids) if source_ids is not None else None
        if selected_sources is not None and any(not source.strip() for source in selected_sources):
            raise ValueError("candidate source IDs must be non-empty")
        source_filter = "AND source = ANY(%s)" if selected_sources is not None else ""
        params: tuple[Any, ...] = (list(sorted(NEWS_EVENT_TYPES)),)
        if selected_sources is not None:
            params += (list(selected_sources),)
        params += (limit, offset)
        cursor = self.connection.cursor()
        cursor.execute(
            f"""
            SELECT event_id, source, source_type, source_ref, url, published_at,
                   observed_at, event_at, fetched_at, processed_at, event_type,
                   entities, symbols, headline, summary, importance, sentiment,
                   impact_horizon, status, reason_code, confidence, content_hash,
                   parser_version, raw_reference, provenance
            FROM phase6_news_events
            WHERE event_type <> ALL(%s) AND status <> 'ERROR'
            {source_filter}
            -- Monotonic primary-key paging prevents fresh arrivals from
            -- shifting already-scanned candidates between bounded polls.
            ORDER BY id ASC
            LIMIT %s
            OFFSET %s
            """,
            params,
        )
        return tuple(_news_event_from_row(row) for row in cursor.fetchall())

    def has_ai_execution(self, execution_id: UUID) -> bool:
        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT 1 FROM phase6_ai_usage WHERE execution_id = %s",
            (execution_id,),
        )
        return cursor.fetchone() is not None

    def latest_ai_execution_state(
        self, request_hash: str
    ) -> tuple[str, str | None, datetime | None, int] | None:
        """Return latest semantic state and durable attempt count for a request."""
        cursor = self.connection.cursor()
        cursor.execute(
            """
            SELECT a.status, a.error_code, max(u.recorded_at), count(u.id)
            FROM phase6_ai_analyses a
            LEFT JOIN phase6_ai_usage u ON u.analysis_id = a.id
            WHERE a.request_hash = %s
            GROUP BY a.id, a.status, a.error_code
            """,
            (request_hash,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]) if row[1] is not None else None, row[2], int(row[3])

    def insert_ai_analysis(
        self,
        request: AIRequest,
        result: AIResult,
        *,
        event_kind: str,
        event_id: str,
        processed_at: datetime,
    ) -> int | None:
        """Persist a legacy generic analysis identity without overwriting it.

        Contract V1 runtime code must use
        ``persist_news_classification_execution`` so evidence validation,
        extraction rows, and usage linkage commit atomically.
        """
        usage = result.usage
        values = (
            event_kind,
            event_id,
            request.request_hash,
            request.context_hash,
            result.provider or request.provider,
            result.model or request.model,
            request.purpose,
            request.prompt_id,
            request.prompt_version,
            request.schema_version,
            request.model_policy_version,
            result.status.value,
            result.error_code.value if result.error_code else None,
            self._json(result.output, "ai_structured_output") if result.output is not None else None,
            result.cache_hit,
            result.retry_count,
            usage.latency_ms if usage else None,
            _utc(processed_at, "processed_at"),
        )
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO phase6_ai_analyses (
                event_kind, event_id, request_hash, input_context_hash, provider,
                model, purpose, prompt_id, prompt_version, schema_version,
                model_policy_version, status, error_code, response_json, cache_hit,
                retry_count, latency_ms, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (request_hash) DO NOTHING
            RETURNING id
            """,
            values,
        )
        fetchone = getattr(cursor, "fetchone", None)
        row = fetchone() if fetchone else None
        if row is not None:
            return int(row[0])
        if fetchone is None:
            return None
        cursor.execute(
            """
            SELECT id, event_kind, event_id, input_context_hash, provider, model,
                   purpose, prompt_id, prompt_version, schema_version,
                   model_policy_version, status, response_json
            FROM phase6_ai_analyses WHERE request_hash = %s
            """,
            (request.request_hash,),
        )
        existing = cursor.fetchone()
        expected = (
            event_kind, event_id, request.context_hash, result.provider or request.provider,
            result.model or request.model, request.purpose, request.prompt_id,
            request.prompt_version, request.schema_version, request.model_policy_version,
            result.status.value,
        )
        expected_output = dict(result.output) if result.output is not None else None
        if (
            existing is None
            or tuple(existing[1:12]) != expected
            or existing[12] != expected_output
        ):
            raise RuntimeError("conflicting Phase 6 analysis request identity")
        return int(existing[0])

    def insert_ai_extractions(self, analysis_id: int, rows: Iterable[dict[str, Any]]) -> int:
        if analysis_id <= 0:
            raise ValueError("analysis_id must be positive")
        values = [
            (
                analysis_id,
                str(row["field_name"]),
                self._json(row.get("field_value"), "field_value") if row.get("field_value") is not None else None,
                self._json(row.get("evidence_refs", ()), "evidence_refs"),
                row.get("confidence"),
                str(row.get("status", "NOT_AVAILABLE")),
                _utc(row["processed_at"], "processed_at"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase6_ai_extractions (
                analysis_id, field_name, field_value, evidence_refs,
                confidence, status, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (analysis_id, field_name) DO UPDATE SET
                field_value=EXCLUDED.field_value, evidence_refs=EXCLUDED.evidence_refs,
                confidence=EXCLUDED.confidence, status=EXCLUDED.status,
                processed_at=EXCLUDED.processed_at
            """,
            values,
        )
        return len(values)

    def insert_ai_usage(
        self,
        request: AIRequest,
        result: AIResult,
        *,
        recorded_at: datetime,
        budget_decision: str,
    ) -> int:
        usage = result.usage or type("EmptyUsage", (), {
            "input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
            "total_tokens": None, "estimated_cost": None, "latency_ms": None,
        })()
        timestamp = _utc(recorded_at, "recorded_at")
        execution_id = uuid5(
            NAMESPACE_URL,
            f"phase6-legacy-usage:{request.request_hash}:{timestamp.isoformat()}",
        )
        values = (
            execution_id, request.request_hash, request.context_hash, result.provider or request.provider,
            result.model or request.model, request.purpose, usage.input_tokens,
            usage.cached_input_tokens, usage.output_tokens, usage.total_tokens,
            usage.estimated_cost, usage.latency_ms, result.status.value, result.retry_count,
            result.cache_hit, budget_decision, result.error_code.value if result.error_code else None,
            timestamp,
        )
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO phase6_ai_usage (
                analysis_id, execution_id, request_hash, input_context_hash, provider, model, purpose,
                input_tokens, cached_input_tokens, output_tokens, total_tokens,
                estimated_cost, latency_ms, status, retry_count, cache_hit,
                budget_decision, error_code, recorded_at
            ) SELECT a.id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
              FROM phase6_ai_analyses a WHERE a.request_hash = %s
            """,
            (*values, request.request_hash),
        )
        if getattr(cursor, "rowcount", 1) == 0:
            raise RuntimeError("AI usage requires its persisted analysis row")
        return 1

    def persist_news_classification_execution(
        self,
        request: AIRequest,
        result: AIResult,
        *,
        event_id_hash: str,
        validated: ValidatedNewsClassification | None,
        execution_id: UUID,
        task_status: Any,
        recorded_at: datetime,
        budget_decision: str,
        retry_attempt: bool = False,
    ) -> int:
        """Atomically write one semantic analysis and one logical usage execution.

        Callers must only pass validated structured output. Existing identities
        are read back and compared; replay never overwrites an accepted result.
        """
        import re

        if not re.fullmatch(r"[0-9a-f]{64}", event_id_hash):
            raise ValueError("event_id_hash must be lowercase SHA-256")
        if result.request_hash != request.request_hash:
            raise ValueError("AI result does not belong to request")
        if validated is None and result.status.value == "AVAILABLE":
            raise ValueError("available News classification requires evidence validation")
        if validated is not None:
            expected = {
                "event_type": validated.event_type,
                "evidence_ids": [reference.evidence_id for reference in validated.evidence_refs],
            }
            if result.output != expected:
                raise ValueError("validated classification differs from structured response")

        status = getattr(task_status, "value", str(task_status))
        processed_at = _utc(recorded_at, "recorded_at")
        output = self._json(dict(result.output), "ai_structured_output") if validated is not None else None
        references = [item.to_persisted_dict() for item in validated.evidence_refs] if validated else []
        usage = result.usage
        provider = result.provider or request.provider
        model = result.model or request.model

        # psycopg Connection.transaction() is required for all-or-nothing
        # analysis/extraction/usage semantics; never silently emulate it.
        transaction = getattr(self.connection, "transaction", None)
        if transaction is None:
            raise TypeError("connection must provide transaction()")
        with transaction():
            cursor = self.connection.cursor()
            cursor.execute(
                """
                INSERT INTO phase6_ai_analyses (
                    event_kind, event_id, request_hash, input_context_hash, provider,
                    model, purpose, prompt_id, prompt_version, schema_version,
                    model_policy_version, status, error_code, response_json, cache_hit,
                    retry_count, latency_ms, processed_at
                ) VALUES ('news', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s)
                ON CONFLICT (request_hash) DO NOTHING
                RETURNING id
                """,
                (
                    event_id_hash, request.request_hash, request.context_hash, provider, model,
                    request.purpose, request.prompt_id, request.prompt_version,
                    request.schema_version, request.model_policy_version, status,
                    result.error_code.value if result.error_code else None, output,
                    result.retry_count, usage.latency_ms if usage else None, processed_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT id, event_id, input_context_hash, provider, model,
                           purpose, prompt_id, prompt_version, schema_version,
                           model_policy_version, status, error_code, response_json
                    FROM phase6_ai_analyses WHERE request_hash = %s
                    """,
                    (request.request_hash,),
                )
                existing = cursor.fetchone()
                expected_identity = (
                    event_id_hash, request.context_hash, provider, model, request.purpose,
                    request.prompt_id, request.prompt_version, request.schema_version,
                    request.model_policy_version,
                )
                if existing is None or tuple(existing[1:10]) != expected_identity:
                    raise RuntimeError("conflicting Phase 6 analysis idempotency identity")
                analysis_id = int(existing[0])
                existing_status, existing_error, existing_response = existing[10:13]
                new_error = result.error_code.value if result.error_code else None
                expected_response = dict(result.output) if validated is not None else None
                same_result = (
                    existing_status == status
                    and existing_error == new_error
                    and existing_response == expected_response
                )
                recoverable = retry_attempt and existing_error in _RETRYABLE_AI_ERROR_CODES
                if recoverable and not same_result:
                    cursor.execute(
                        """
                        UPDATE phase6_ai_analyses
                        SET status = %s, error_code = %s, response_json = %s,
                            cache_hit = %s, retry_count = %s, latency_ms = %s,
                            processed_at = %s
                        WHERE id = %s
                        """,
                        (
                            status, new_error, output, result.cache_hit, result.retry_count,
                            usage.latency_ms if usage else None, processed_at, analysis_id,
                        ),
                    )
                elif not same_result:
                    raise RuntimeError("conflicting Phase 6 analysis response for request_hash")
            else:
                analysis_id = int(row[0])

            if validated is not None:
                cursor.execute(
                    """
                    INSERT INTO phase6_ai_extractions (
                        analysis_id, field_name, field_value, evidence_refs,
                        confidence, status, processed_at
                    ) VALUES (%s, 'ai_event_type_candidate', %s, %s, NULL, %s, %s)
                    ON CONFLICT (analysis_id, field_name) DO NOTHING
                    RETURNING id
                    """,
                    (
                        analysis_id,
                        self._json(validated.event_type, "ai_event_type_candidate")
                        if validated.event_type is not None else None,
                        self._json(references, "ai_evidence_refs"), status, processed_at,
                    ),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        """
                        SELECT field_value, evidence_refs, confidence, status
                        FROM phase6_ai_extractions
                        WHERE analysis_id = %s AND field_name = 'ai_event_type_candidate'
                        """,
                        (analysis_id,),
                    )
                    existing_extraction = cursor.fetchone()
                    if existing_extraction is None or (
                        existing_extraction[0] != validated.event_type
                        or existing_extraction[1] != references
                        or existing_extraction[2] is not None
                        or existing_extraction[3] != status
                    ):
                        raise RuntimeError("conflicting Phase 6 extraction for analysis")

            cursor.execute(
                """
                INSERT INTO phase6_ai_usage (
                    analysis_id, execution_id, request_hash, input_context_hash,
                    provider, model, purpose, input_tokens, cached_input_tokens,
                    output_tokens, total_tokens, estimated_cost, latency_ms, status,
                    retry_count, cache_hit, budget_decision, error_code, recorded_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (execution_id) DO NOTHING
                RETURNING id
                """,
                (
                    analysis_id, execution_id, request.request_hash, request.context_hash,
                    provider, model, request.purpose,
                    usage.input_tokens if usage else None,
                    usage.cached_input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    usage.total_tokens if usage else None,
                    usage.estimated_cost if usage else None,
                    usage.latency_ms if usage else None, status, result.retry_count,
                    result.cache_hit, budget_decision,
                    result.error_code.value if result.error_code else None, processed_at,
                ),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    "SELECT analysis_id, request_hash FROM phase6_ai_usage WHERE execution_id = %s",
                    (execution_id,),
                )
                existing_usage = cursor.fetchone()
                if existing_usage != (analysis_id, request.request_hash):
                    raise RuntimeError("conflicting Phase 6 usage execution identity")
        return analysis_id

    def insert_prompt_version(
        self,
        definition: PromptDefinition,
        *,
        prompt_hash: str,
        created_at: datetime,
        status: str = "ACTIVE",
    ) -> int:
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO phase6_prompt_versions (
                prompt_id, prompt_version, schema_version, model_policy_version,
                prompt_hash, status, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (prompt_id, prompt_version, schema_version) DO NOTHING
            RETURNING prompt_hash, model_policy_version, status
            """,
            (
                definition.prompt_id, definition.prompt_version, definition.schema_version,
                definition.model_policy_version, prompt_hash, status, _utc(created_at, "created_at"),
            ),
        )
        fetchone = getattr(cursor, "fetchone", None)
        row = fetchone() if fetchone else None
        if row is None:
            if fetchone is None:
                return 1
            cursor.execute(
                """
                SELECT prompt_hash, model_policy_version, status
                FROM phase6_prompt_versions
                WHERE prompt_id = %s AND prompt_version = %s AND schema_version = %s
                """,
                (definition.prompt_id, definition.prompt_version, definition.schema_version),
            )
            row = cursor.fetchone()
            if row is None or row[0] != prompt_hash or row[1] != definition.model_policy_version:
                raise RuntimeError("conflicting Phase 6 prompt version identity")
            if row[2] == "RETIRED":
                raise RuntimeError("retired Phase 6 prompt version cannot be reactivated")
            if row[2] == "DRAFT" and status == "ACTIVE":
                cursor.execute(
                    """
                    UPDATE phase6_prompt_versions SET status = 'ACTIVE'
                    WHERE prompt_id = %s AND prompt_version = %s AND schema_version = %s
                      AND prompt_hash = %s AND status = 'DRAFT'
                    """,
                    (definition.prompt_id, definition.prompt_version, definition.schema_version, prompt_hash),
                )
        elif row[0] != prompt_hash or row[1] != definition.model_policy_version or row[2] != status:
            raise RuntimeError("Phase 6 prompt version was not registered as requested")
        return 1

    def cleanup(self, table: str, cutoff: datetime, *, batch_size: int = 1000, max_batches: int = 1) -> int:
        if table not in _RETENTION_TABLES:
            raise ValueError("unsupported Phase 6 retention table")
        if batch_size <= 0 or max_batches <= 0:
            raise ValueError("retention batch bounds must be positive")
        time_column = "recorded_at" if table == "phase6_ai_usage" else "processed_at"
        total = 0
        cursor = self.connection.cursor()
        for _ in range(max_batches):
            cursor.execute(
                f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {time_column} < %s ORDER BY {time_column} LIMIT %s)",
                (_utc(cutoff, "cutoff"), batch_size),
            )
            changed = max(0, int(getattr(cursor, "rowcount", 0)))
            total += changed
            if changed < batch_size:
                break
        return total


def cleanup_phase6(repository: Phase6Repository, settings: Any, *, now: datetime) -> dict[str, int]:
    """Apply configured retention in bounded batches for all mutable Phase 6 data."""
    linked_ai_retention_days = max(
        int(settings.phase6_ai_analysis_retention_days),
        int(settings.phase6_ai_usage_retention_days),
    )
    policies = {
        "phase6_news_events": settings.phase6_news_retention_days,
        "phase6_macro_events": settings.phase6_macro_retention_days,
        "phase6_unlock_events": settings.phase6_unlock_retention_days,
        "phase6_ai_usage": settings.phase6_ai_usage_retention_days,
        "phase6_ai_extractions": linked_ai_retention_days,
        "phase6_ai_analyses": linked_ai_retention_days,
    }
    return {
        table: repository.cleanup(table, now - timedelta(days=int(days)), batch_size=1000, max_batches=10)
        for table, days in policies.items()
    }


def _news_event_from_row(row: tuple[Any, ...]) -> NewsEvent:
    (
        event_id, source, source_type, source_ref, url, published_at, observed_at,
        event_at, fetched_at, processed_at, event_type, entities, symbols, headline,
        summary, importance, sentiment, impact_horizon, status, reason_code,
        confidence, content_hash, parser_version, raw_value, provenance_value,
    ) = row
    provenance_data = dict(provenance_value or {})
    raw_data = dict(raw_value) if raw_value is not None else None
    raw_reference = None
    if raw_data:
        raw_reference = RawReference(
            reference=str(raw_data["reference"]),
            content_hash=str(raw_data["content_hash"]),
            byte_size=raw_data.get("byte_size"),
            expires_at=_parse_utc(raw_data.get("expires_at")),
        )
    provenance = Provenance(
        source_id=str(provenance_data.get("source_id", source)),
        source_ref=str(provenance_data.get("source_ref", source_ref)),
        url=provenance_data.get("url", url),
        content_hash=str(provenance_data.get("content_hash", content_hash)),
        observed_at=_parse_utc(provenance_data.get("observed_at", observed_at)),
        fetched_at=_parse_utc(provenance_data.get("fetched_at", fetched_at)),
        processed_at=_parse_utc(provenance_data.get("processed_at", processed_at)),
        parser_version=str(provenance_data.get("parser_version", parser_version)),
        schema_version=str(provenance_data.get("schema_version", "phase6.news.v1")),
        normalization_version=str(provenance_data.get("normalization_version", "phase6.normalization.v1")),
        model_id=provenance_data.get("model_id"),
        prompt_version=provenance_data.get("prompt_version"),
        extraction_version=provenance_data.get("extraction_version"),
    )
    return NewsEvent(
        event_id=str(event_id), source=str(source), source_type=SourceType(str(source_type)),
        source_ref=str(source_ref), url=url,
        published_at=_parse_utc(published_at), observed_at=_parse_utc(observed_at),
        event_at=_parse_utc(event_at), fetched_at=_parse_utc(fetched_at),
        processed_at=_parse_utc(processed_at), event_type=str(event_type),
        entities=tuple(entities or ()), symbols=tuple(symbols or ()),
        summary=str(summary or ""), importance=Importance(str(importance)),
        status=EventStatus(str(status)), confidence=confidence,
        content_hash=str(content_hash), parser_version=str(parser_version),
        raw_reference=raw_reference, provenance=provenance,
        reason_code=reason_code, sentiment=sentiment, headline=str(headline),
        impact_horizon=impact_horizon,
    )


def _parse_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("persisted Phase 6 timestamp is not UTC")
    return value.astimezone(timezone.utc)
