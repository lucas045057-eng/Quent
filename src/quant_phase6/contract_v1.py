"""Frozen Phase 6 AI Runtime Contract V1 primitives.

Only the News event-type candidate task is registered here. This module has no
provider, network, environment, database, or filesystem dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from html.parser import HTMLParser
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .ai import AIRequest
from .contracts import EventStatus, NewsEvent
from .prompts import NEWS_CLASSIFICATION_PROMPTS, PromptRegistry
from .security import AISafeContext, build_safe_context


TASK_ID = "phase6.news.event_classification"
PURPOSE = "news_classification"
PROMPT_ID = "phase6.news.event_classification"
PROMPT_VERSION = "v1"
INPUT_SCHEMA_ID = "phase6.news.event-classification.input.v1"
OUTPUT_SCHEMA_ID = "phase6.news.event-classification.output.v1"
CONTRACT_SCHEMA_VERSION = "phase6.news.event-classification.contract.v1"
NEWS_EVENT_TYPES = frozenset({
    "LISTING", "DELISTING", "PROTOCOL_UPDATE", "SECURITY", "PARTNERSHIP",
    "REGULATION", "EXCHANGE_ANNOUNCEMENT", "GENERAL_MARKET_CONTEXT",
})


def _freeze_contract_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_contract_json(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_contract_json(child) for child in value)
    return value
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class TaskReason(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NO_PLAIN_TEXT_EVIDENCE = "NO_PLAIN_TEXT_EVIDENCE"
    NO_CLASSIFICATION = "NO_CLASSIFICATION"
    STALE_INPUT = "STALE_INPUT"
    SOFT_BUDGET_DEGRADED = "SOFT_BUDGET_DEGRADED"
    HARD_BUDGET_STOP = "HARD_BUDGET_STOP"
    QUEUE_FULL = "QUEUE_FULL"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_EXHAUSTED = "TRANSPORT_EXHAUSTED"
    AUTHENTICATION = "AUTHENTICATION"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    POLICY_BLOCK = "POLICY_BLOCK"
    INVALID_JSON = "INVALID_JSON"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    EVIDENCE_ERROR = "EVIDENCE_ERROR"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_id: str
    purpose: str
    prompt_id: str
    prompt_version: str
    input_schema_id: str
    output_schema_id: str
    contract_schema_version: str
    mandatory: bool = False


NEWS_CLASSIFICATION_TASK = TaskDefinition(
    task_id=TASK_ID,
    purpose=PURPOSE,
    prompt_id=PROMPT_ID,
    prompt_version=PROMPT_VERSION,
    input_schema_id=INPUT_SCHEMA_ID,
    output_schema_id=OUTPUT_SCHEMA_ID,
    contract_schema_version=CONTRACT_SCHEMA_VERSION,
    mandatory=False,
)


class TaskRegistry:
    """An intentionally closed registry; V1 cannot be extended ad hoc."""

    def __init__(self, definitions: tuple[TaskDefinition, ...] | None = None) -> None:
        definitions = definitions or (NEWS_CLASSIFICATION_TASK,)
        if definitions != (NEWS_CLASSIFICATION_TASK,):
            raise ValueError("Phase 6 Contract V1 registers exactly one frozen task")
        self._definitions = {item.task_id: item for item in definitions}

    @classmethod
    def v1(cls) -> "TaskRegistry":
        return cls((NEWS_CLASSIFICATION_TASK,))

    def require(self, task_id: str) -> TaskDefinition:
        try:
            return self._definitions[task_id]
        except KeyError as exc:
            raise KeyError("Phase 6 task is not registered") from exc

    def all(self) -> tuple[TaskDefinition, ...]:
        return tuple(self._definitions.values())


def phase9_jev_task_id(evaluation_id: UUID) -> str:
    """Return the per-evaluation Phase 9 task identity without extending V1 registry."""
    if not isinstance(evaluation_id, UUID) or evaluation_id.int == 0:
        raise ValueError("Phase 9 Jev evaluation_id must be a non-nil UUID")
    return f"phase9-jev:{evaluation_id}"


class EvidenceContractError(ValueError):
    """The provider output is malformed or cannot be traced to task evidence."""


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    event_id_hash: str
    source_id: str
    source_ref_hash: str
    content_hash: str
    field_path: str
    field_hash: str
    span_unit: str
    span_start: int
    span_end: int
    source_timestamp: datetime | None
    observed_at: datetime

    def to_persisted_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "event_id_hash": self.event_id_hash,
            "source_id": self.source_id,
            "source_ref_hash": self.source_ref_hash,
            "content_hash": self.content_hash,
            "field_path": self.field_path,
            "field_hash": self.field_hash,
            "span_unit": self.span_unit,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "source_timestamp": _iso_utc(self.source_timestamp),
            "observed_at": _iso_utc(self.observed_at),
        }


@dataclass(frozen=True, slots=True)
class PreparedNewsClassification:
    event_id_hash: str
    source_id: str
    content_hash: str
    context: AISafeContext
    input_payload: Mapping[str, Any]
    evidence_catalog: Mapping[str, EvidenceReference]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_payload", _freeze_contract_json(self.input_payload))
        object.__setattr__(self, "evidence_catalog", MappingProxyType(dict(self.evidence_catalog)))


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    task_id: str
    event_id_hash: str
    input_context_hash: str
    status: EventStatus
    reason_code: TaskReason | None
    request_hash: str | None = None
    execution_id: UUID | None = None
    candidate: str | None = None
    evidence_refs: tuple[EvidenceReference, ...] = ()
    provider: str | None = None
    model: str | None = None
    usage: Any | None = None


@dataclass(frozen=True, slots=True)
class ValidatedNewsClassification:
    event_type: str | None
    evidence_refs: tuple[EvidenceReference, ...]


def build_news_classification_context(
    event: NewsEvent,
    *,
    now: datetime,
    max_age: timedelta = timedelta(hours=24),
) -> PreparedNewsClassification | TaskOutcome:
    """Build the exact task-only evidence projection or a pre-request outcome."""
    if not isinstance(event, NewsEvent):
        raise TypeError("Contract V1 accepts NewsEvent only")
    now = _require_utc(now, "now")
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")

    # Construct the established allowlisted context first, then derive the
    # classifier-specific projection from that DTO rather than serializing the
    # normalized event or ORM row directly.
    base = build_safe_context(event, task_id=TASK_ID)
    fields = base.allowed_fields
    event_id_hash = _sha256(event.event_id)
    anchor = event.published_at or event.observed_at
    if event.status is EventStatus.STALE or now - _require_utc(anchor, "source timestamp") > max_age:
        return _pre_request_outcome(
            event_id_hash, _empty_context_hash(), EventStatus.STALE, TaskReason.STALE_INPUT
        )
    if fields.get("event_type") in NEWS_EVENT_TYPES:
        return _pre_request_outcome(
            event_id_hash, _empty_context_hash(), EventStatus.NOT_AVAILABLE, TaskReason.NOT_APPLICABLE
        )

    source_ref = event.provenance.source_ref
    if source_ref != event.source_ref or event.provenance.source_id != event.source:
        raise EvidenceContractError("canonical event provenance identity mismatch")
    source_ref_hash = _sha256(source_ref)
    selected: list[tuple[str, str]] = []
    for field_name, field_path, limit in (
        ("headline", "/headline", 2048),
        ("summary", "/summary", 8192),
    ):
        value = fields.get(field_name)
        if isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= limit and _is_plain_text(value):
            selected.append((field_path, value))

    if not selected:
        return _pre_request_outcome(
            event_id_hash, _empty_context_hash(), EventStatus.NOT_AVAILABLE, TaskReason.NO_PLAIN_TEXT_EVIDENCE
        )

    evidence_catalog: dict[str, EvidenceReference] = {}
    evidence: list[dict[str, str]] = []
    for field_path, value in selected[:2]:
        field_hash = _sha256(value)
        identity = {
            "event_id_hash": event_id_hash,
            "content_hash": event.content_hash,
            "field_path": field_path,
            "field_hash": field_hash,
        }
        evidence_id = _sha256(_canonical_json(identity).decode("utf-8"))
        ref = EvidenceReference(
            evidence_id=evidence_id,
            event_id_hash=event_id_hash,
            source_id=event.source,
            source_ref_hash=source_ref_hash,
            content_hash=event.content_hash,
            field_path=field_path,
            field_hash=field_hash,
            span_unit="UTF8_BYTE",
            span_start=0,
            span_end=len(value.encode("utf-8")),
            source_timestamp=event.published_at,
            observed_at=event.observed_at,
        )
        evidence_catalog[evidence_id] = ref
        evidence.append({"evidence_id": evidence_id, "field_path": field_path, "text": value})

    payload: dict[str, Any] = {"evidence": evidence}
    payload = validate_news_classification_input(payload)
    canonical = _canonical_json({"task_id": TASK_ID, "input": payload})
    context_hash = hashlib.sha256(canonical).hexdigest()
    safe_context = AISafeContext(
        task_id=TASK_ID,
        allowed_fields=payload,
        context_hash=context_hash,
    )
    return PreparedNewsClassification(
        event_id_hash=event_id_hash,
        source_id=event.source,
        content_hash=event.content_hash,
        context=safe_context,
        input_payload=payload,
        evidence_catalog=MappingProxyType(evidence_catalog),
    )


def validate_news_classification_input(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"evidence"}:
        raise EvidenceContractError("input must contain only evidence")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 2:
        raise EvidenceContractError("evidence must contain one or two items")
    total_bytes = len(_canonical_json(value))
    if total_bytes > 12_288:
        raise EvidenceContractError("task input exceeds the 12 KiB byte limit")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, Mapping) or set(item) != {"evidence_id", "field_path", "text"}:
            raise EvidenceContractError("evidence item has an invalid shape")
        evidence_id, field_path, text = item["evidence_id"], item["field_path"], item["text"]
        if not isinstance(evidence_id, str) or not _HASH_RE.fullmatch(evidence_id):
            raise EvidenceContractError("evidence_id must be lowercase SHA-256")
        if evidence_id in seen:
            raise EvidenceContractError("duplicate evidence_id")
        seen.add(evidence_id)
        if field_path not in {"/headline", "/summary"} or not isinstance(text, str) or not text:
            raise EvidenceContractError("evidence path or text is invalid")
        limit = 2048 if field_path == "/headline" else 8192
        if len(text.encode("utf-8")) > limit or not _is_plain_text(text):
            raise EvidenceContractError("evidence field violates text bounds")
        normalized.append({"evidence_id": evidence_id, "field_path": field_path, "text": text})
    return {"evidence": normalized}


def validate_news_classification_output(
    value: Mapping[str, Any],
    prepared: PreparedNewsClassification,
) -> ValidatedNewsClassification:
    if not isinstance(value, Mapping) or set(value) != {"event_type", "evidence_ids"}:
        raise EvidenceContractError("output must contain exactly event_type and evidence_ids")
    event_type = value["event_type"]
    evidence_ids = value["evidence_ids"]
    if event_type is not None and (not isinstance(event_type, str) or event_type not in NEWS_EVENT_TYPES):
        raise EvidenceContractError("unsupported News event type candidate")
    if not isinstance(evidence_ids, list) or len(evidence_ids) > 2:
        raise EvidenceContractError("evidence_ids must be an array of at most two values")
    if any(not isinstance(item, str) or not _HASH_RE.fullmatch(item) for item in evidence_ids):
        raise EvidenceContractError("invalid evidence id")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise EvidenceContractError("duplicate evidence id")
    if event_type is None and evidence_ids:
        raise EvidenceContractError("null event_type requires empty evidence_ids")
    if event_type is not None and not evidence_ids:
        raise EvidenceContractError("a factual candidate requires evidence")
    refs: list[EvidenceReference] = []
    for evidence_id in evidence_ids:
        ref = prepared.evidence_catalog.get(evidence_id)
        if ref is None or ref.event_id_hash != prepared.event_id_hash or ref.content_hash != prepared.content_hash:
            raise EvidenceContractError("evidence id does not belong to this request")
        refs.append(ref)
    return ValidatedNewsClassification(event_type, tuple(refs))


class NewsClassificationOutputSchema:
    """Strict closed-schema validator used by the provider-neutral Gateway."""

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {"event_type", "evidence_ids"}:
            raise EvidenceContractError("output must contain exactly event_type and evidence_ids")
        event_type = value["event_type"]
        evidence_ids = value["evidence_ids"]
        if event_type is not None and (not isinstance(event_type, str) or event_type not in NEWS_EVENT_TYPES):
            raise EvidenceContractError("unsupported News event type candidate")
        if not isinstance(evidence_ids, list) or len(evidence_ids) > 2:
            raise EvidenceContractError("evidence_ids must be an array of at most two values")
        if any(not isinstance(item, str) or not _HASH_RE.fullmatch(item) for item in evidence_ids):
            raise EvidenceContractError("invalid evidence id")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise EvidenceContractError("duplicate evidence id")
        if event_type is None and evidence_ids:
            raise EvidenceContractError("null event_type requires empty evidence_ids")
        if event_type is not None and not evidence_ids:
            raise EvidenceContractError("a factual candidate requires evidence")
        return {"event_type": event_type, "evidence_ids": list(evidence_ids)}


def make_news_classification_request(
    prepared: PreparedNewsClassification,
    *,
    provider: str,
    model: str,
    timeout_seconds: float = 10.0,
    max_output_bytes: int = 32_768,
    prompts: PromptRegistry = NEWS_CLASSIFICATION_PROMPTS,
) -> AIRequest:
    task = TaskRegistry.v1().require(TASK_ID)
    definition = prompts.require(task.prompt_id, task.prompt_version, task.contract_schema_version)
    if definition.review_status != "APPROVED_FOR_IMPLEMENTATION":
        raise ValueError("unreviewed prompt cannot be enabled")
    canonical_context = _canonical_json({"task_id": task.task_id, "input": prepared.context.to_dict()})
    if hashlib.sha256(canonical_context).hexdigest() != prepared.context.context_hash:
        raise EvidenceContractError("prepared News context hash does not match immutable input")
    if prepared.input_payload != prepared.context.allowed_fields:
        raise EvidenceContractError("prepared News input does not match safe context")
    envelope = prompts.render(
        task.prompt_id, prepared.context,
        prompt_version=task.prompt_version,
        schema_version=task.contract_schema_version,
    )
    return AIRequest(
        purpose=task.purpose,
        prompt_id=task.prompt_id,
        prompt_version=task.prompt_version,
        schema_version=task.contract_schema_version,
        model_policy_version=definition.model_policy_version,
        provider=provider,
        model=model,
        envelope=envelope,
        context_hash=prepared.context.context_hash,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


def pre_request_outcome(
    prepared: PreparedNewsClassification,
    reason: TaskReason,
    *,
    status: EventStatus = EventStatus.NOT_AVAILABLE,
) -> TaskOutcome:
    return _pre_request_outcome(
        prepared.event_id_hash,
        prepared.context.context_hash,
        status,
        reason,
    )


def stable_execution_id(event_id_hash: str, request_hash: str, attempt: int = 1) -> UUID:
    if not _HASH_RE.fullmatch(event_id_hash) or not _HASH_RE.fullmatch(request_hash):
        raise ValueError("execution identity requires SHA-256 inputs")
    if attempt <= 0:
        raise ValueError("execution attempt must be positive")
    return uuid5(NAMESPACE_URL, f"{TASK_ID}:{event_id_hash}:{request_hash}:attempt:{attempt}")


def _pre_request_outcome(
    event_id_hash: str,
    context_hash: str,
    status: EventStatus,
    reason: TaskReason,
) -> TaskOutcome:
    return TaskOutcome(TASK_ID, event_id_hash, context_hash, status, reason)


def _empty_context_hash() -> str:
    return hashlib.sha256(_canonical_json({"task_id": TASK_ID, "input": {"evidence": []}})).hexdigest()


class _MarkupDetector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.found_markup = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found_markup = True

    def handle_endtag(self, tag: str) -> None:
        self.found_markup = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found_markup = True

    def handle_comment(self, data: str) -> None:
        self.found_markup = True

    def handle_decl(self, decl: str) -> None:
        self.found_markup = True

    def handle_pi(self, data: str) -> None:
        self.found_markup = True


def _is_plain_text(value: str) -> bool:
    for char in value:
        codepoint = ord(char)
        if (codepoint < 32 and char not in "\t\n\r") or 0x7F <= codepoint <= 0x9F:
            return False
    detector = _MarkupDetector()
    try:
        detector.feed(value)
        detector.close()
    except Exception:
        return False
    return not detector.found_markup


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime | None) -> str | None:
    return None if value is None else _require_utc(value, "timestamp").isoformat().replace("+00:00", "Z")
