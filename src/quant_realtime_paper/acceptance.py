"""Formal long-run acceptance evaluation over operational cycle provenance."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def evaluate_formal_acceptance(
    session: Mapping[str, Any] | None,
    cycles: Sequence[Mapping[str, Any]],
    decision_sources: Sequence[str],
    *,
    minimum_duration_seconds: float,
) -> dict[str, Any]:
    if minimum_duration_seconds <= 0:
        raise ValueError("minimum_duration_seconds must be positive")
    blockers: set[str] = set()
    if session is None:
        return {
            "status": "FAIL",
            "passed": False,
            "blockers": ["NO_SESSION"],
            "minimum_duration_seconds": minimum_duration_seconds,
            "observed_seconds": 0.0,
            "cycle_count": 0,
            "decision_count": 0,
            "source_counts": {},
            "max_cycle_gap_seconds": None,
            "allowed_cycle_gap_seconds": None,
            "structured_readiness_cycle_count": 0,
            "no_trade_classification_counts": {},
        }

    started = _parse_time(session.get("started_at"))
    ended = _parse_time(session.get("ended_at"))
    observed_seconds = max(0.0, (ended - started).total_seconds()) if started and ended else 0.0
    if session.get("state") != "STOPPED" or ended is None:
        blockers.add("SESSION_NOT_STOPPED")
    if started is None or ended is None or observed_seconds < minimum_duration_seconds:
        blockers.add("DURATION_INCOMPLETE")

    source_counts: dict[str, int] = {}
    for cycle in cycles:
        source = str(cycle.get("data_source") or "UNKNOWN")
        source_counts[source] = source_counts.get(source, 0) + 1
    non_real_cycle = any(source != "REAL_PUBLIC_DATA" for source in source_counts)
    non_real_decision = any(source != "REAL_PUBLIC_DATA" for source in decision_sources)
    if not cycles or non_real_cycle or not decision_sources or non_real_decision:
        blockers.add("NON_REAL_DATA_SOURCE")
    if any(cycle.get("readiness") != "PAPER READY"
           or cycle.get("final_action") != "PAPER LOOP READY" for cycle in cycles):
        blockers.add("PAPER_NOT_READY")

    poll_seconds = session.get("poll_seconds")
    try:
        allowed_gap = max(60.0, float(poll_seconds) * 2)
    except (TypeError, ValueError):
        allowed_gap = 60.0
    cycle_times = sorted(
        parsed for cycle in cycles
        if (parsed := _parse_time(cycle.get("observed_at"))) is not None
    )
    max_gap: float | None = None
    if started and ended and cycle_times:
        gaps = [
            (cycle_times[0] - started).total_seconds(),
            (ended - cycle_times[-1]).total_seconds(),
        ]
        gaps.extend((right - left).total_seconds()
                    for left, right in zip(cycle_times, cycle_times[1:]))
        max_gap = max(gaps, default=0.0)
        if max_gap > allowed_gap:
            blockers.add("CYCLE_GAP_EXCEEDED")
    else:
        blockers.add("CYCLE_COVERAGE_MISSING")

    structured_readiness = [
        detail for cycle in cycles
        if isinstance((detail := cycle.get("readiness_detail")), Mapping)
    ]
    no_trade_classification_counts: dict[str, int] = {}
    for detail in structured_readiness:
        classification = detail.get("no_trade_classification")
        if isinstance(classification, str) and classification:
            no_trade_classification_counts[classification] = (
                no_trade_classification_counts.get(classification, 0) + 1
            )

    result = {
        "status": "PASS" if not blockers else "FAIL",
        "passed": not blockers,
        "blockers": sorted(blockers),
        "session_id": session.get("session_id"),
        "minimum_duration_seconds": minimum_duration_seconds,
        "observed_seconds": round(observed_seconds, 3),
        "cycle_count": len(cycles),
        "decision_count": len(decision_sources),
        "source_counts": source_counts,
        "real_public_data_only": bool(cycles) and not non_real_cycle and bool(decision_sources) and not non_real_decision,
        "max_cycle_gap_seconds": round(max_gap, 3) if max_gap is not None else None,
        "allowed_cycle_gap_seconds": allowed_gap,
        "structured_readiness_cycle_count": len(structured_readiness),
        "no_trade_classification_counts": no_trade_classification_counts,
    }
    return result
