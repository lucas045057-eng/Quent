"""Fail-closed readiness and safety predicates for the realtime paper monitor."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    status: str
    final_action: str
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperV1ReadinessV1:
    process_ready: bool
    data_ready: bool
    stage1_ready: bool
    phase9_ready: bool
    policy_ready: bool
    risk_ready: bool
    paper_ready: bool
    reconciliation_ready: bool
    execution_ready: bool
    blockers: tuple[str, ...]
    no_trade_classification: str
    reason_code: str
    status: str
    final_action: str


_PAPER_V1_FLAGS = (
    "process_ready", "data_ready", "stage1_ready", "phase9_ready",
    "policy_ready", "risk_ready", "paper_ready",
    "reconciliation_ready", "execution_ready",
)
_MISSING_TTL_REASONS = frozenset({
    "INTAKE_TTL_NOT_CONFIGURED", "MISSING_CANDIDATE_TTL", "MISSING_EXECUTION_TTL",
})
_POLICY_DISABLED_REASONS = frozenset({"POLICY_DISABLED", "NO_ENABLED_PATTERNS"})
_STRATEGY_NO_TRADE_REASONS = frozenset({"PHASE9_INSUFFICIENT", "STAGE1_NOT_ELIGIBLE", "NO_MATCHED_PATTERN"})


# Reason codes that mean "no entry candidate is available at this instant"
# rather than "the observation loop is unhealthy".  The strategy-level reasons
# are the canonical benign no-trades; the remainder are producer/consumer
# timing properties of the durable Phase 9 pipeline (the engine emits the
# candidate event and completes its three-horizon evaluation on its own
# schedule).  A healthy loop must not be reported NOT READY merely because a
# candidate has not been materialised yet.  Genuine faults still fail closed:
# a raised exception, PHASE9_EVALUATION_FAILED, a configuration/market-data
# guard or an identity/integrity mismatch is never in this set.
BENIGN_NO_ENTRY_REASONS = _STRATEGY_NO_TRADE_REASONS | frozenset({
    "STAGE1_CANDIDATE_NOT_DURABLE",
    "PHASE9_RESULT_NOT_FOUND",
    "PHASE9_NOT_COMPLETE",
})


def classify_no_trade_reason(failure_stage: str | None, reason_code: str | None) -> str:
    reason = reason_code or "PAPER_READINESS_INCOMPLETE"
    if reason in _MISSING_TTL_REASONS:
        return "NO_TRADE_BY_MISSING_TTL"
    if reason in _POLICY_DISABLED_REASONS:
        return "NO_TRADE_BY_POLICY_DISABLED"
    if failure_stage == "strategy" or reason in _STRATEGY_NO_TRADE_REASONS:
        return "NO_TRADE_BY_STRATEGY"
    if failure_stage == "risk":
        return "NO_TRADE_BY_RISK_REJECT"
    if failure_stage == "data" or reason.startswith(("STALE_", "INVALID_MARKET_DATA")):
        return "NO_TRADE_BY_STALE_DATA"
    return "NO_TRADE_BY_SYSTEM_NOT_READY"


def assess_paper_v1_readiness(
    checks: Mapping[str, bool], *, failure_stage: str | None = None,
    reason_code: str | None = None,
) -> PaperV1ReadinessV1:
    """Retain each Paper readiness boundary and classify its fail-closed outcome."""
    values = {name: checks.get(name) is True for name in _PAPER_V1_FLAGS}
    execution_ready = values["execution_ready"] and all(
        value for name, value in values.items() if name != "execution_ready"
    )
    values["execution_ready"] = execution_ready
    blockers = tuple(name for name in _PAPER_V1_FLAGS if not values[name])
    ready = not blockers
    reason = reason_code or ("PAPER_EXECUTION_READY" if ready else "PAPER_READINESS_INCOMPLETE")
    classification = (
        "PAPER_READY" if ready else classify_no_trade_reason(failure_stage, reason)
    )
    return PaperV1ReadinessV1(
        **values, blockers=blockers, no_trade_classification=classification,
        reason_code=reason, status="PAPER READY" if ready else "PAPER NOT READY",
        final_action="PAPER LOOP READY" if ready else "DO NOT TRADE",
    )


def classify_public_data_source(observations: Sequence[Mapping[str, object]]) -> str:
    if not observations:
        return "UNKNOWN"
    classes: set[str] = set()
    for item in observations:
        exchange = str(item.get("exchange") or "").strip().lower()
        source = str(item.get("source") or "").strip().lower()
        marker = f"{exchange} {source}"
        if any(word in marker for word in ("fixture", "synthetic", "replay", "historical")):
            classes.add("SYNTHETIC_FIXTURE")
        elif exchange == "bitget" and source in {"bitget_v3_rest", "bitget_v3_ws", "phase2:bitget"}:
            classes.add("REAL_PUBLIC_DATA")
        else:
            classes.add("UNKNOWN")
    if classes == {"REAL_PUBLIC_DATA"}:
        return "REAL_PUBLIC_DATA"
    if "REAL_PUBLIC_DATA" in classes and ("SYNTHETIC_FIXTURE" in classes or "UNKNOWN" in classes):
        return "MIXED_REJECTED"
    if classes == {"SYNTHETIC_FIXTURE"}:
        return "SYNTHETIC_FIXTURE"
    return "UNKNOWN"


def assess_readiness(checks: Mapping[str, bool]) -> ReadinessResult:
    blockers = tuple(sorted(name for name, ready in checks.items() if ready is not True))
    ready = not blockers
    return ReadinessResult(
        status="PAPER READY" if ready else "PAPER NOT READY",
        final_action="PAPER LOOP READY" if ready else "DO NOT TRADE",
        blockers=blockers,
    )


def _finite_positive(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def safety_violations(
    *, market_fresh: bool, database_ok: bool, reconciliation_ok: bool,
    price: object, quantity: object, duplicate_intent: bool, clock_ok: bool,
    paper_healthy: bool, live_disabled: bool,
) -> tuple[str, ...]:
    violations: list[str] = []
    if not market_fresh:
        violations.append("STALE_MARKET_DATA")
    if not database_ok:
        violations.append("DATABASE_UNAVAILABLE")
    if not reconciliation_ok:
        violations.append("RECONCILIATION_MISMATCH")
    if not _finite_positive(price):
        violations.append("INVALID_PRICE")
    if not _finite_positive(quantity):
        violations.append("INVALID_QUANTITY")
    if duplicate_intent:
        violations.append("DUPLICATE_INTENT")
    if not clock_ok:
        violations.append("CLOCK_ANOMALY")
    if not paper_healthy:
        violations.append("PAPER_ENGINE_UNHEALTHY")
    if not live_disabled:
        violations.append("LIVE_NOT_DISABLED")
    return tuple(violations)


class ClockMonitor:
    """Compare UTC wall-clock movement with monotonic movement."""

    def __init__(self, *, tolerance_seconds: float = 2.0) -> None:
        if tolerance_seconds < 0:
            raise ValueError("tolerance_seconds must be non-negative")
        self.tolerance_seconds = tolerance_seconds
        self._wall: datetime | None = None
        self._mono: float | None = None

    def observe(self, wall: datetime, monotonic: float) -> bool:
        if wall.tzinfo is None or wall.utcoffset() is None:
            raise ValueError("wall time must be timezone-aware")
        if self._wall is None or self._mono is None:
            self._wall, self._mono = wall, monotonic
            return True
        wall_delta = (wall - self._wall).total_seconds()
        mono_delta = monotonic - self._mono
        self._wall, self._mono = wall, monotonic
        return wall_delta >= 0 and mono_delta >= 0 and abs(wall_delta - mono_delta) <= self.tolerance_seconds


def retry_delay(attempt: int, *, base_seconds: float = 1.0, maximum_seconds: float = 60.0) -> float:
    if isinstance(attempt, bool) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if base_seconds <= 0 or maximum_seconds <= 0:
        raise ValueError("retry delays must be positive")
    return min(maximum_seconds, base_seconds * (2 ** min(attempt - 1, 30)))
