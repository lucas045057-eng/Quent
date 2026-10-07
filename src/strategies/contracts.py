"""Immutable, versioned strategy inputs. Missing values never imply neutrality."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

Horizon = Literal["1_3H", "3_8H", "8_24H"]
Side = Literal["LONG", "SHORT"]
Availability = Literal["AVAILABLE", "UNAVAILABLE", "NOT_CONFIGURED", "ERROR", "PARTIAL"]
Freshness = Literal["FRESH", "DEGRADED", "STALE", "UNKNOWN"]
Coverage = Literal["COMPLETE", "PARTIAL", "UNKNOWN"]

def discard_retired_news_mode(value):
    """Read old settings without mutating inputs or restoring retired gates."""
    if isinstance(value, dict):
        value = dict(value)
        value.pop("news_mode", None)
    return value

class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "STRATEGY_V2"

    @property
    def digest(self) -> str:
        body=json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",",":"), allow_nan=False)
        return sha256(body.encode()).hexdigest()

    @field_validator("*")
    @classmethod
    def valid_scalars(cls, value):
        if isinstance(value,datetime) and (value.tzinfo is None or value.utcoffset().total_seconds()!=0):
            raise ValueError("UTC timestamp required")
        if isinstance(value,Decimal) and not value.is_finite():
            raise ValueError("finite Decimal required")
        return value

class MarketObservation(Record):
    symbol: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_group: str = ""
    value: Decimal | None = None
    unit: str | None = None
    source_event_time: datetime | None = None
    observed_at: datetime | None = None
    fetched_at: datetime | None = None
    processed_at: datetime | None = None
    availability: Availability = "UNAVAILABLE"
    freshness: Freshness = "UNKNOWN"
    quality: Literal["VALID","PARTIAL","UNKNOWN","INVALID"] = "UNKNOWN"
    coverage: Coverage = "UNKNOWN"
    source_digest: str | None = None
    reason: str | None = None
    source_context: dict | None = None
    revalidated_at: datetime | None = None

    @model_serializer(mode='wrap')
    def serialize_context(self, handler):
        # Preserve historical V2 digests when the optional context is absent.
        result=handler(self)
        if self.source_context is None:result.pop('source_context',None)
        if self.revalidated_at is None:result.pop('revalidated_at',None)
        return result

    @field_validator('source_context')
    @classmethod
    def bounded_context(cls, value):
        if value is not None:
            # Deep-frozen projections hand back MappingProxyType, which is a
            # valid mapping but is not serializable by json.dumps alone. Measure
            # the same tree without re-encoding frozen wrappers. The value is
            # returned untouched, so no fact digest can change.
            def plain(node):
                if isinstance(node,Mapping):return {str(key):plain(item) for key,item in node.items()}
                if isinstance(node,(tuple,list)):return [plain(item) for item in node]
                if node is None or isinstance(node,(str,int,float,bool)):return node
                return str(node)
            if len(json.dumps(plain(value),ensure_ascii=False,allow_nan=False).encode())>32768:
                raise ValueError('source context exceeds bound')
        return value

    @model_validator(mode="after")
    def status_and_value(self):
        if self.availability in {"UNAVAILABLE","NOT_CONFIGURED","ERROR"} and self.value is not None:
            raise ValueError("unavailable observation cannot carry a value")
        if (self.value is None)!=(self.unit is None):
            raise ValueError("value requires an explicit unit")
        return self

    @property
    def usable(self) -> bool:
        return (self.availability=="AVAILABLE" and self.freshness=="FRESH"
            and self.quality=="VALID" and self.coverage=="COMPLETE"
            and self.value is not None and self.observed_at is not None and bool(self.source_group))

class EvidenceNode(Record):
    observation: MarketObservation
    role: Literal["SUPPORTING","CONTRADICTING","NEUTRAL","MISSING","STALE"]
    interpretation: str = Field(min_length=1)
    depends_on: tuple[str,...] = ()
    hypothesis: str = ""

    @model_validator(mode="after")
    def valid_role(self):
        if self.role in {"SUPPORTING","CONTRADICTING","NEUTRAL"} and not self.observation.usable:
            raise ValueError("unusable evidence cannot confirm or imply neutrality")
        return self

class TriggerRule(Record):
    side: Side
    price: Decimal = Field(gt=0)
    operator: Literal["GTE","LTE"]
    required_kinds: tuple[str,...] = ("PRICE",)
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def directional_operator(self):
        if self.operator!=("GTE" if self.side=="LONG" else "LTE"):
            raise ValueError("trigger direction/operator mismatch")
        return self

class WaitingTrigger(Record):
    symbol: str
    trigger: TriggerRule
    invalidation_price: Decimal = Field(gt=0)
    distance_ratio: Decimal = Field(ge=0)
    generated_at: datetime
    status: Literal["WAITING","EXPIRED","UNAVAILABLE","INVALIDATED"]="WAITING"

class ScreeningCandidate(Record):
    symbol: str
    category: Literal["A","B","C","D"]
    reason_codes: tuple[str,...]
    direction: Side | None = None
    structure: str = "UNKNOWN"
    trigger: TriggerRule | None = None
    invalidation_price: Decimal | None = None
    potential_space_ratio: Decimal | None = None
    turnover: Decimal | None = None
    spread_bps: Decimal | None = None
    evidence: tuple[EvidenceNode,...] = ()

class ScreeningBatch(Record):
    generated_at: datetime
    universe: tuple[str,...]
    candidates: tuple[ScreeningCandidate,...]
    top_candidates: tuple[ScreeningCandidate,...]
    waiting_triggers: tuple[WaitingTrigger,...]=()
    strategy_version: str="QUANT_PAPER_V2"
    policy_digest: str
    market_digest: str

    @model_validator(mode="after")
    def capacity(self):
        if len(self.top_candidates)>5 or any(c.category!="A" for c in self.top_candidates):
            raise ValueError("only up to five A candidates may enter analysis")
        if len({c.symbol for c in self.candidates})!=len(self.candidates):
            raise ValueError("duplicate screening symbol")
        return self

class AnalysisRequest(Record):
    candidate: ScreeningCandidate
    requested_at: datetime
    deadline: datetime
    screening_digest: str
    required_kinds: tuple[str,...]=("PRICE","PERP_FLOW","SPOT_FLOW","OI","FUNDING","BENCHMARK")

    @model_validator(mode="before")
    @classmethod
    def discard_retired_event_requirements(cls,value):
        value = discard_retired_news_mode(value)
        if isinstance(value,dict):
            retired={'EVENT_COVERAGE','EXCHANGE_EVENT_COVERAGE','MACRO_COVERAGE'}
            if 'required_kinds' in value:value['required_kinds']=tuple(k for k in value['required_kinds'] if k not in retired)
        return value

    @model_validator(mode="after")
    def admitted(self):
        if self.candidate.category!="A" or self.deadline<=self.requested_at:
            raise ValueError("fresh analysis requires an A candidate and a positive deadline")
        return self

class AnalysisSnapshot(Record):
    symbol: str
    requested_at: datetime
    captured_at: datetime
    screening_digest: str
    observations: tuple[MarketObservation,...]
    refresh_status: Literal["REFRESHED","UNAVAILABLE","PARTIAL"]
    data_source: Literal["REAL_PUBLIC_DATA","SYNTHETIC_FIXTURE","UNKNOWN"]
    candidate: ScreeningCandidate

    @model_validator(mode="after")
    def fresh_identity(self):
        if self.captured_at<self.requested_at or self.candidate.symbol!=self.symbol:
            raise ValueError("analysis identity/clock mismatch")
        if any(o.symbol not in {self.symbol,"BTCUSDT","ETHUSDT","GLOBAL"} for o in self.observations):
            raise ValueError("foreign-symbol evidence")
        return self

class EvidenceChain(Record):
    hypothesis: str
    direction: Side | None
    nodes: tuple[EvidenceNode,...]
    required_kinds: tuple[str,...]
    missing_kinds: tuple[str,...]
    contradictions: tuple[str,...]
    confirmed: bool
    explanation: str
    advisories: tuple[str,...]=()

class Scenario(Record):
    kind: Literal["UP","DOWN","RANGE"]
    conditions: tuple[str,...]
    trigger: TriggerRule | None=None

class StructuredTradeThesis(Record):
    symbol: str
    horizon: Horizon
    timeframe: Literal["15m","1H","4H"]
    generated_at: datetime
    valid_until: datetime
    snapshot_digest: str
    bias: Literal["LONG","SHORT","RANGE","WAIT"]
    market_structure: str
    confidence: Literal["HIGH","MEDIUM","LOW","INSUFFICIENT"]
    evidence_chain: EvidenceChain | None=None
    contradictions: tuple[str,...]=()
    support_levels: tuple[Decimal,...]=()
    resistance_levels: tuple[Decimal,...]=()
    scenarios: tuple[Scenario,...]=()
    trigger: TriggerRule | None=None
    invalidation_price: Decimal | None=None
    target_price: Decimal | None=None
    risk_events: tuple[str,...]=()
    no_trade_reason: str | None=None
    source_refs: tuple[str,...]=()
    summary: str=""
    strategy_version: str="QUANT_PAPER_V2"
    # Legacy serialized field; zero means no adjustment, never a risk-status fact.
    news_confidence_penalty: Literal[0,1]=0
    advisories: tuple[str,...]=()

    @model_validator(mode="after")
    def executable_geometry(self):
        if self.valid_until<=self.generated_at or len(self.snapshot_digest)!=64:
            raise ValueError("invalid thesis validity/digest")
        if self.bias in {"LONG","SHORT"}:
            if (self.trigger is None or self.trigger.side!=self.bias
                or self.invalidation_price is None or self.target_price is None):
                raise ValueError("directional thesis needs numeric trade geometry")
            sign=1 if self.bias=="LONG" else -1
            if sign*(self.trigger.price-self.invalidation_price)<=0 or sign*(self.target_price-self.trigger.price)<=0:
                raise ValueError("invalid directional geometry")
        if self.scenarios and {s.kind for s in self.scenarios}!={"UP","DOWN","RANGE"}:
            raise ValueError("all three scenarios required")
        return self

class ExecutionPolicyResult(Record):
    disposition: Literal["PASS","NO_TRADE","WAITING_FOR_TRIGGER"]
    reason_codes: tuple[str,...]
    evaluated_at: datetime
    policy_digest: str
    thesis: StructuredTradeThesis | None=None
    snapshot_digest: str | None=None
    recheck_digest: str | None=None
    @model_validator(mode="after")
    def bound_pass(self):
        if self.disposition=="PASS" and (self.thesis is None or not self.snapshot_digest or not self.recheck_digest):
            raise ValueError("PASS requires a bound thesis and current evidence")
        return self
