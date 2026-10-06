"""Context-only enrichment for Phase 1 screening results.

Phase 6 is deliberately additive: the original ``Stage1Result`` is kept as
an immutable input and the external context is carried beside it. Nothing in
this module can change Stage 1 eligibility or emit a trading action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from quant_phase1.stage1 import Stage1Result

from .contracts import EventStatus


_STATUS_ORDER = {
    EventStatus.ERROR: 5,
    EventStatus.STALE: 4,
    EventStatus.PARTIAL: 3,
    EventStatus.NOT_AVAILABLE: 2,
    EventStatus.AVAILABLE: 1,
}
_SENSITIVE_REFERENCE_KEYS = {"raw_payload", "payload", "headers", "cookies", "token"}


def _status_of(value: Any) -> EventStatus:
    raw = getattr(value, "status", None)
    if raw is None and isinstance(value, Mapping):
        raw = value.get("status")
    if raw is None:
        return EventStatus.NOT_AVAILABLE
    try:
        return EventStatus(str(raw))
    except ValueError:
        return EventStatus.ERROR


def _bounded_refs(values: Sequence[Any] | None, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
    if values is None:
        return ()
    result: list[dict[str, Any]] = []
    for value in tuple(values)[:limit]:
        if not isinstance(value, Mapping):
            continue
        result.append(_scrub_reference(value))
    return tuple(result)


def _scrub_reference(value: Mapping[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text.lower() in _SENSITIVE_REFERENCE_KEYS:
            continue
        if isinstance(item, Mapping):
            cleaned[key_text] = _scrub_reference(item)
        elif isinstance(item, (list, tuple)):
            cleaned[key_text] = tuple(
                _scrub_reference(child) if isinstance(child, Mapping) else child
                for child in item
            )
        else:
            cleaned[key_text] = item
    return cleaned


def _aggregate_status(groups: Sequence[Sequence[Any]]) -> EventStatus:
    statuses = [_status_of(value) for group in groups for value in group]
    if not statuses:
        return EventStatus.NOT_AVAILABLE
    if EventStatus.ERROR in statuses:
        return EventStatus.ERROR
    if EventStatus.STALE in statuses:
        return EventStatus.STALE
    if EventStatus.PARTIAL in statuses:
        return EventStatus.PARTIAL
    if EventStatus.NOT_AVAILABLE in statuses:
        return EventStatus.PARTIAL if EventStatus.AVAILABLE in statuses else EventStatus.NOT_AVAILABLE
    return max(statuses, key=lambda status: _STATUS_ORDER[status])


@dataclass(frozen=True, slots=True)
class Phase6Stage1Context:
    screening_run_id: int
    symbol: str
    status: EventStatus
    news_refs: tuple[dict[str, Any], ...]
    macro_refs: tuple[dict[str, Any], ...]
    unlock_refs: tuple[dict[str, Any], ...]
    ai_refs: tuple[dict[str, Any], ...]
    processed_at: datetime
    result: Stage1Result
    context_only: bool = True

    def __post_init__(self) -> None:
        if self.screening_run_id <= 0:
            raise ValueError("screening_run_id must be positive")
        if self.symbol != self.result.symbol:
            raise ValueError("context symbol must match Stage1 result")
        if not self.context_only:
            raise ValueError("Phase 6 context must remain context_only")
        if self.processed_at.tzinfo is None or self.processed_at.utcoffset() is None:
            raise ValueError("processed_at must be timezone-aware")


def build_stage1_context(
    result: Stage1Result,
    *,
    screening_run_id: int,
    news_refs: Sequence[Any] = (),
    macro_refs: Sequence[Any] = (),
    unlock_refs: Sequence[Any] = (),
    ai_results: Sequence[Any] = (),
    processed_at: datetime,
) -> Phase6Stage1Context:
    news = _bounded_refs(news_refs)
    macro = _bounded_refs(macro_refs)
    unlock = _bounded_refs(unlock_refs)
    ai = _bounded_refs(ai_results)
    return Phase6Stage1Context(
        screening_run_id=screening_run_id,
        symbol=result.symbol,
        status=_aggregate_status((news, macro, unlock, ai)),
        news_refs=news,
        macro_refs=macro,
        unlock_refs=unlock,
        ai_refs=ai,
        processed_at=processed_at,
        result=result,
    )


class Phase6ContextRuntime:
    """Build bounded, ordered context without mutating Stage 1 results."""

    def build(
        self,
        results: Sequence[Stage1Result],
        *,
        screening_run_id: int,
        refs_by_symbol: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        processed_at: datetime,
    ) -> tuple[Phase6Stage1Context, ...]:
        refs_by_symbol = refs_by_symbol or {}
        output: list[Phase6Stage1Context] = []
        for result in results:
            refs = refs_by_symbol.get(result.symbol, {})
            output.append(
                build_stage1_context(
                    result,
                    screening_run_id=screening_run_id,
                    news_refs=refs.get("news", ()),
                    macro_refs=refs.get("macro", ()),
                    unlock_refs=refs.get("unlock", ()),
                    ai_results=refs.get("ai", ()),
                    processed_at=processed_at,
                )
            )
        return tuple(output)
