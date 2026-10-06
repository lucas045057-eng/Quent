"""Context-only Phase 7 enrichment for existing Stage1 results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import json
from typing import Any, Iterable, Mapping
from itertools import islice

from quant_phase1.contracts import DataStatus

_MAX_REFERENCE_BYTES = 8 * 1024
_MAX_CANDIDATES = 2_000
_MAX_CONTEXTS_PER_SYMBOL = 32
_FORBIDDEN_DECISION_FIELDS = frozenset({
    "category", "classification", "decision", "score", "eligibility",
    "side", "signal", "order", "position",
})


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")
    return value


def _status(value: Any) -> str:
    try:
        normalized = value.value if hasattr(value, "value") else value
        if normalized not in {item.value for item in DataStatus} | {"PARTIAL"}:
            raise ValueError
        return str(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Phase 7 context status") from exc


def _json_size(value: Any, field: str) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > _MAX_REFERENCE_BYTES:
        raise ValueError(f"{field} exceeds bounded size")
    lowered = encoded.lower()
    if any(f'"{field_name}"' in lowered for field_name in _FORBIDDEN_DECISION_FIELDS):
        raise ValueError(f"{field} contains a Stage1 decision field")


@dataclass(frozen=True, slots=True)
class Phase7Context:
    source_id: str
    status: Any
    reference: str
    coverage: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("source_id is required")
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("context reference is required")
        if len(self.reference.encode("utf-8")) > 2048:
            raise ValueError("context reference exceeds bounded size")
        _status(self.status)
        if not isinstance(self.coverage, Decimal) or not self.coverage.is_finite() or not Decimal("0") <= self.coverage <= Decimal("1"):
            raise ValueError("context coverage must be a Decimal between zero and one")


def _aggregate_status(contexts: tuple[Phase7Context, ...]) -> tuple[str, str]:
    if not contexts:
        return DataStatus.NOT_AVAILABLE.value, "PHASE7_CONTEXT_NOT_AVAILABLE"
    statuses = tuple(_status(context.status) for context in contexts)
    if DataStatus.ERROR.value in statuses:
        return DataStatus.ERROR.value, "PHASE7_CONTEXT_ERROR"
    if DataStatus.STALE.value in statuses:
        return DataStatus.STALE.value, "PHASE7_CONTEXT_STALE"
    if DataStatus.NOT_AVAILABLE.value in statuses:
        return "PARTIAL", "PHASE7_CONTEXT_PARTIAL"
    if any(status != DataStatus.AVAILABLE.value for status in statuses):
        return "PARTIAL", "PHASE7_CONTEXT_PARTIAL"
    return DataStatus.AVAILABLE.value, "COMPLETE"


def build_stage1_phase7_enrichment(
    *,
    screening_run_id: int,
    candidates: Iterable[Any],
    contexts: Mapping[str, Iterable[Phase7Context]],
    processed_at: datetime,
    normalization_version: str = "phase7-enrichment-v1",
) -> tuple[dict[str, Any], ...]:
    if isinstance(screening_run_id, bool) or not isinstance(screening_run_id, int) or screening_run_id < 0:
        raise ValueError("screening_run_id must be non-negative")
    processed_at = _utc(processed_at, "processed_at")
    bounded_candidates = tuple(islice(candidates, _MAX_CANDIDATES + 1))
    if len(bounded_candidates) > _MAX_CANDIDATES:
        raise ValueError("Stage1 candidate batch exceeds bounded cap")
    rows = []
    for candidate in bounded_candidates:
        symbol = getattr(candidate, "symbol", None)
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Stage1 candidate symbol is required")
        context_items = tuple(islice(contexts.get(symbol, ()), _MAX_CONTEXTS_PER_SYMBOL + 1))
        if len(context_items) > _MAX_CONTEXTS_PER_SYMBOL:
            raise ValueError("Phase 7 contexts per symbol exceed bounded cap")
        if any(not isinstance(item, Phase7Context) for item in context_items):
            raise ValueError("contexts must contain Phase7Context values")
        indexed: dict[str, dict[str, str]] = {}
        for item in context_items:
            if item.source_id in indexed:
                raise ValueError("duplicate Phase7 context source")
            indexed[item.source_id] = {
                "reference": item.reference,
                "status": _status(item.status),
                "coverage": str(item.coverage),
            }
        context_reference = {"sources": indexed}
        status, reason = _aggregate_status(context_items)
        coverage = {
            "sample_count": len(context_items),
            "source_count": len(context_items),
            "available_count": sum(_status(item.status) == DataStatus.AVAILABLE.value for item in context_items),
            "missing_count": sum(_status(item.status) != DataStatus.AVAILABLE.value for item in context_items),
            "coverage_ratio": str(
                sum((item.coverage for item in context_items), Decimal("0")) / Decimal(len(context_items))
                if context_items else Decimal("0")
            ),
        }
        _json_size(context_reference, "context_reference")
        _json_size(coverage, "coverage")
        rows.append({
            "screening_run_id": screening_run_id,
            "symbol": symbol,
            "context_reference": context_reference,
            "coverage": coverage,
            "status": status,
            "reason": reason,
            "normalization_version": normalization_version,
            "created_at": processed_at,
            "processed_at": processed_at,
        })
    return tuple(rows)

