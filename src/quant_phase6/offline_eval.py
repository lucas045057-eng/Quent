"""Deterministic, provider-free evaluation for Phase 6 structured outputs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence

from .ai import StrictSchema


@dataclass(frozen=True, slots=True)
class OfflineCase:
    case_id: str
    input_context: Mapping[str, Any]
    expected_event_type: str | None = None
    expected_entities: tuple[str, ...] | None = None
    expected_evidence_refs: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class OfflineEvaluation:
    total_cases: int
    schema_compliant: int
    entity_matches: int
    entity_cases: int
    source_fidelity: int
    fidelity_cases: int
    unsupported_claims: int
    average_latency_ms: float
    total_tokens: int
    estimated_cost: Decimal

    @property
    def schema_compliance_rate(self) -> float:
        return self.schema_compliant / self.total_cases if self.total_cases else 0.0

    @property
    def unsupported_claim_rate(self) -> float:
        return self.unsupported_claims / self.total_cases if self.total_cases else 0.0


class OfflineEvalHarness:
    """Evaluate a local fixture evaluator without network or provider state."""

    def __init__(self, schema: StrictSchema, evaluator: Callable[[OfflineCase], Any]) -> None:
        self.schema = schema
        self.evaluator = evaluator

    def run(self, cases: Sequence[OfflineCase]) -> OfflineEvaluation:
        cases = tuple(cases)
        if len(cases) > 10_000:
            raise ValueError("offline evaluation is bounded to 10000 cases")
        schema_compliant = 0
        entity_matches = 0
        entity_cases = 0
        source_fidelity = 0
        fidelity_cases = 0
        unsupported_claims = 0
        total_tokens = 0
        estimated_cost = Decimal("0")
        latencies: list[float] = []

        for case in cases:
            started = perf_counter()
            response = self.evaluator(case)
            elapsed_ms = (perf_counter() - started) * 1000
            output, usage, response_latency = _response_parts(response)
            latency = response_latency if response_latency is not None else elapsed_ms
            latencies.append(float(latency))
            if usage is not None:
                total_tokens += usage.total_tokens or 0
                estimated_cost += usage.estimated_cost or Decimal("0")
            try:
                validated = self.schema.validate(output)
            except (TypeError, ValueError):
                continue
            schema_compliant += 1
            unsupported_claims += _unsupported_claims(case, validated)
            if case.expected_entities is not None:
                entity_cases += 1
                if tuple(validated.get("entities", ())) == case.expected_entities:
                    entity_matches += 1
            if case.expected_event_type is not None or case.expected_evidence_refs is not None:
                fidelity_cases += 1
                if _fidelity_matches(case, validated):
                    source_fidelity += 1

        average = sum(latencies) / len(latencies) if latencies else 0.0
        return OfflineEvaluation(
            total_cases=len(cases),
            schema_compliant=schema_compliant,
            entity_matches=entity_matches,
            entity_cases=entity_cases,
            source_fidelity=source_fidelity,
            fidelity_cases=fidelity_cases,
            unsupported_claims=unsupported_claims,
            average_latency_ms=round(average, 3),
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
        )


def _response_parts(response: Any) -> tuple[Mapping[str, Any], Any, float | None]:
    if hasattr(response, "structured_output"):
        usage = getattr(response, "usage", None)
        latency = getattr(response, "latency_ms", None)
        if latency is None and usage is not None:
            latency = getattr(usage, "latency_ms", None)
        return response.structured_output, usage, latency
    if not isinstance(response, Mapping):
        raise TypeError("offline evaluator must return a mapping or AI response")
    return response, None, None


def _unsupported_claims(case: OfflineCase, output: Mapping[str, Any]) -> int:
    count = 0
    if case.expected_event_type is not None and output.get("event_type") != case.expected_event_type:
        count += 1
    if case.expected_evidence_refs is not None:
        expected = set(case.expected_evidence_refs)
        count += len(set(output.get("evidence_refs", ())) - expected)
    return count


def _fidelity_matches(case: OfflineCase, output: Mapping[str, Any]) -> bool:
    if case.expected_event_type is not None and output.get("event_type") != case.expected_event_type:
        return False
    if case.expected_evidence_refs is not None:
        if not set(output.get("evidence_refs", ())).issubset(set(case.expected_evidence_refs)):
            return False
    return True
