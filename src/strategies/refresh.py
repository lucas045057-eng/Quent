"""A fresh second-stage receipt is mandatory; screening receipts are never reused."""
from datetime import datetime, timezone
from typing import Protocol
from .contracts import AnalysisRequest, AnalysisSnapshot, MarketObservation
from .canonical_validation import fresh_receipt

MAX_AGE_SECONDS={"PRICE":5,"PERP_FLOW":305,"SPOT_FLOW":305,"OI":600,"FUNDING":600,"BENCHMARK":60,"CROSS_OI":3600,"CROSS_FUNDING":3600,"UNLOCK":86400}

class CandidateRefreshSource(Protocol):
    data_source: str
    def refresh(self, request: AnalysisRequest) -> tuple[MarketObservation,...]: ...

def refresh_candidate(request, *, source, now=None):
    observations=tuple(source.refresh(request))
    captured=now or datetime.now(timezone.utc)
    if captured<request.requested_at: raise ValueError("refresh clock precedes request")
    checked=[]
    for observation in observations:
        if observation.symbol not in {request.candidate.symbol,"BTCUSDT","ETHUSDT","GLOBAL"}:
            raise ValueError("refresh returned foreign symbol")
        if (not fresh_receipt(observation,requested_at=request.requested_at,captured_at=captured)
            or observation.fetched_at is None
            or observation.fetched_at>captured or observation.observed_at is None
            or observation.observed_at>captured or captured>request.deadline
            or (captured-observation.observed_at).total_seconds()>MAX_AGE_SECONDS.get(observation.kind,{"15m":900,"1H":3600,"4H":14400}.get(observation.kind.split(":")[-1],60))):
            observation=observation.model_copy(update={"freshness":"STALE","reason":"REFRESH_RECEIPT_INVALID"})
        checked.append(observation)
    kinds={o.kind for o in checked if o.usable}
    for kind in request.required_kinds:
        if not any(o.kind==kind for o in checked):
            checked.append(MarketObservation(symbol=request.candidate.symbol,kind=kind,provider="refresh",
                source_ref="refresh:missing:"+kind,reason="REFRESH_REQUIRED_SOURCE_UNAVAILABLE"))
    complete=set(request.required_kinds)<=kinds
    status="REFRESHED" if complete else "PARTIAL" if kinds else "UNAVAILABLE"
    return AnalysisSnapshot(symbol=request.candidate.symbol,requested_at=request.requested_at,
        captured_at=captured,screening_digest=request.screening_digest,observations=tuple(checked),
        refresh_status=status,data_source=source.data_source,candidate=request.candidate)
