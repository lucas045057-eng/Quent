"""Phase 2 enrichment boundary; it never rewrites Phase 1 classification."""

from __future__ import annotations

from dataclasses import dataclass

from quant_phase1.stage1 import Stage1Result

from .contracts import CrossExchangeSnapshot, DataStatus


@dataclass(frozen=True, slots=True)
class Stage1DerivativeEnrichment:
    phase1_result: Stage1Result
    derivative_snapshot: CrossExchangeSnapshot | None
    derivative_status: DataStatus
    context_only: bool = True

    @property
    def classification(self) -> str:
        return self.phase1_result.classification


def enrich_stage1_candidate(
    result: Stage1Result, snapshot: CrossExchangeSnapshot | None
) -> Stage1DerivativeEnrichment:
    if snapshot is None:
        return Stage1DerivativeEnrichment(result, None, DataStatus.NOT_AVAILABLE)
    return Stage1DerivativeEnrichment(result, snapshot, snapshot.status)

