"""Immutable execution contracts; no framework or exchange client imports."""
from __future__ import annotations

from dataclasses import dataclass, fields, field, MISSING
from datetime import datetime
from decimal import Decimal
import json
from typing import Iterator, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from quant_phase9.canonical import canonical_json, canonical_sha256
from quant_phase9.contracts import DecisionCandidateV1, Sha256Hex


def utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise ValueError('execution time must be UTC')


def decimal(value: Decimal, *, positive=False, nonnegative=False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError('finite Decimal required')
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError('invalid economic amount')


@dataclass(frozen=True, slots=True)
class DecisionEnvelopeV1:
    candidate: DecisionCandidateV1
    effective_status: str
    evaluation_snapshot_hash: str
    input_snapshot_hash: str

    thesis_digest: str | None = None
    analysis_snapshot_digest: str | None = None
    risk_config_digest: str | None = None

    def __post_init__(self):
        for digest in (self.thesis_digest,self.analysis_snapshot_digest,self.risk_config_digest):
            if digest is not None:Sha256Hex(digest)
        if not isinstance(self.candidate, DecisionCandidateV1):
            raise TypeError('frozen DecisionCandidateV1 required')
        Sha256Hex(self.evaluation_snapshot_hash)
        Sha256Hex(self.input_snapshot_hash)


@dataclass(frozen=True, slots=True)
class InstrumentSpecV1:
    core_symbol: str
    canonical_symbol: str
    venue: str
    instrument_id: str
    settlement_currency: str
    quantity_step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    price_tick: Decimal
    min_notional: Decimal
    tradable: bool

    def __post_init__(self):
        for name in ('quantity_step', 'min_quantity', 'max_quantity', 'price_tick', 'min_notional'):
            decimal(getattr(self, name), positive=True)
        if self.max_quantity < self.min_quantity or not all((self.core_symbol,
                self.canonical_symbol, self.venue, self.instrument_id, self.settlement_currency)):
            raise ValueError('invalid instrument binding')
        if type(self.tradable) is not bool:
            raise ValueError('tradable must be boolean')


@dataclass(frozen=True, slots=True)
class QuoteV1:
    canonical_symbol: str
    bid: Decimal
    ask: Decimal
    as_of: datetime
    status: str
    received_at: datetime | None = None
    source_as_of: datetime | None = None
    source: str | None = None
    exchange: str | None = None

    def __post_init__(self):
        decimal(self.bid, positive=True)
        decimal(self.ask, positive=True)
        utc(self.as_of)
        if self.received_at is not None:
            utc(self.received_at)
            if self.received_at < self.as_of:
                raise ValueError('quote receipt precedes captured snapshot')
        if self.source_as_of is not None:
            utc(self.source_as_of)
        if self.ask < self.bid:
            raise ValueError('crossed quote')


@dataclass(frozen=True, slots=True)
class AccountSnapshotV1:
    account_id: str
    venue: str
    mode: str
    equity: Decimal
    available_balance: Decimal
    exposure: Decimal
    reserved_risk: Decimal
    open_intents: int
    as_of: datetime
    reconciliation_status: str

    def __post_init__(self):
        for name in ('equity', 'available_balance', 'exposure', 'reserved_risk'):
            decimal(getattr(self, name), nonnegative=True)
        utc(self.as_of)
        if type(self.open_intents) is not int or self.open_intents < 0 or not self.account_id:
            raise ValueError('invalid account snapshot')


@dataclass(frozen=True, slots=True)
class ExecutionIntentV1:
    intent_id: UUID
    decision_id: UUID
    evaluation_id: UUID
    evaluation_snapshot_hash: str
    input_snapshot_hash: str
    core_symbol: str
    canonical_symbol: str
    instrument_id: str
    venue: str
    account_id: str
    mode: str
    side: str
    created_at: datetime
    valid_until: datetime
    approved_quantity: Decimal
    quantity_unit: str
    reference_price: Decimal
    quote_as_of: datetime
    max_notional: Decimal
    max_margin: Decimal
    max_leverage: Decimal
    entry_type: str
    time_in_force: str
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    stop_price: Decimal
    protection_required: bool
    risk_budget: Decimal
    settlement_currency: str
    risk_policy_version: str
    risk_policy_hash: str
    decision_policy_version: str
    code_version: str
    client_order_id: str
    content_digest: str
    strategy_profile: str | None = field(default=None, metadata={"omit_if_none": True})
    target_price: Decimal | None = field(default=None, metadata={"omit_if_none": True})
    max_hold_seconds: int | None = field(default=None, metadata={"omit_if_none": True})
    setup_id: str | None = field(default=None, metadata={"omit_if_none": True})
    setup_trigger_at: datetime | None = field(default=None, metadata={"omit_if_none": True})

    thesis_digest: str | None = field(default=None, metadata={"omit_if_none": True})
    analysis_snapshot_digest: str | None = field(default=None, metadata={"omit_if_none": True})
    risk_config_digest: str | None = field(default=None, metadata={"omit_if_none": True})
    horizon: str | None = field(default=None, metadata={"omit_if_none": True})
    trade_plan_digest: str | None = field(default=None, metadata={"omit_if_none": True})

    def __post_init__(self):
        strategy_values = (self.strategy_profile, self.target_price, self.max_hold_seconds, self.setup_id, self.setup_trigger_at)
        if any(v is not None for v in strategy_values):
            if any(v is None for v in strategy_values) or self.strategy_profile not in {"QUANT_PAPER_V1_CONSERVATIVE","QUANT_PAPER_V2"}:
                raise ValueError("incomplete or unsupported Paper strategy intent")
            decimal(self.target_price, positive=True)
            utc(self.setup_trigger_at)
            Sha256Hex(self.setup_id)
            if type(self.max_hold_seconds) is not int or self.max_hold_seconds<=0:
                raise ValueError("invalid configured hold")
            if self.strategy_profile=="QUANT_PAPER_V1_CONSERVATIVE" and (self.max_hold_seconds!=10800 or self.core_symbol not in {"BTCUSDT", "ETHUSDT"} or self.max_leverage>1):
                raise ValueError("historical Paper strategy scope invalid")
            if self.setup_trigger_at > self.created_at:
                raise ValueError("Paper strategy intent scope invalid")
            if (self.side == "LONG" and self.target_price <= self.reference_price) or (self.side == "SHORT" and self.target_price >= self.reference_price):
                raise ValueError("target on wrong side")

        bindings=(self.thesis_digest,self.analysis_snapshot_digest,self.risk_config_digest,self.horizon,self.trade_plan_digest)
        if self.strategy_profile=="QUANT_PAPER_V2":
            if any(v is None for v in bindings) or self.horizon not in {"1_3H","3_8H","8_24H"} or self.decision_policy_version!="2.0.0":
                raise ValueError("V2 intent requires complete research and risk binding")
            for digest in (self.thesis_digest,self.analysis_snapshot_digest,self.risk_config_digest,self.trade_plan_digest):Sha256Hex(digest)
        elif any(v is not None for v in bindings):raise ValueError("research binding requires V2 intent")
        for name in ('created_at', 'valid_until', 'quote_as_of'):
            utc(getattr(self, name))
        for name in ('approved_quantity', 'reference_price', 'max_notional', 'max_margin',
                     'max_leverage', 'stop_price', 'risk_budget'):
            decimal(getattr(self, name), positive=True)
        for name in ('max_spread_bps', 'max_slippage_bps'):
            decimal(getattr(self, name), nonnegative=True)
        if self.mode not in {'BACKTEST', 'PAPER'} or self.side not in {'LONG', 'SHORT'}:
            raise ValueError('unsupported execution mode or side')
        if self.entry_type != 'MARKET' or self.time_in_force != 'IOC' or self.quantity_unit != 'BASE':
            raise ValueError('unsupported order capability')
        if self.valid_until <= self.created_at or self.quote_as_of > self.created_at:
            raise ValueError('invalid intent lifetime')
        if self.protection_required is not True:
            raise ValueError('protection required')
        if self.content_digest != intent_digest(self):
            raise ValueError('intent digest mismatch')
        fraction = self.max_slippage_bps / 10000
        worst = self.reference_price * (1 + fraction if self.side == 'LONG' else 1 - fraction)
        if (worst <= 0 or self.max_notional < self.approved_quantity * self.reference_price * (1 + fraction)
            or self.max_margin * self.max_leverage < self.max_notional
            or self.risk_budget < self.approved_quantity * abs(worst - self.stop_price)):
            raise ValueError('intent economic constraints are inconsistent')
        if (self.side == 'LONG' and self.stop_price >= self.reference_price) or (
                self.side == 'SHORT' and self.stop_price <= self.reference_price):
            raise ValueError('stop on wrong side')
        for name in ('evaluation_snapshot_hash', 'input_snapshot_hash', 'risk_policy_hash', 'content_digest'):
            Sha256Hex(getattr(self, name))
        digest = intent_digest(self)
        if digest != self.content_digest or self.intent_id != uuid5(NAMESPACE_URL, 'quant:intent:v1:' + digest):
            raise ValueError('intent digest or identity mismatch')
        if self.client_order_id != 'Q-' + digest[:30]:
            raise ValueError('stable client identity mismatch')


def intent_body(intent: ExecutionIntentV1 | dict) -> dict:
    values = intent if isinstance(intent, dict) else {f.name: getattr(intent, f.name) for f in fields(intent)}
    optional = {f.name for f in fields(ExecutionIntentV1) if f.metadata.get("omit_if_none")}
    return {k: v for k, v in values.items() if k not in {'intent_id', 'content_digest', 'client_order_id'}
            and not (k in optional and v is None)}


def intent_digest(intent: ExecutionIntentV1 | dict) -> str:
    return str(canonical_sha256(intent_body(intent)))


def make_intent(**body) -> ExecutionIntentV1:
    digest = intent_digest(body)
    return ExecutionIntentV1(**body, intent_id=uuid5(NAMESPACE_URL, 'quant:intent:v1:' + digest),
        client_order_id='Q-' + digest[:30], content_digest=digest)


@dataclass(frozen=True, slots=True)
class ExecutionResultV1:
    intent_id: UUID
    execution_id: str
    client_order_id: str
    external_order_refs: tuple[str, ...]
    status: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    average_price: Decimal | None
    fees: Decimal
    fee_currency: str
    funding_cash: Decimal | None
    protection_status: str
    event_time: datetime
    received_at: datetime
    reason: str
    adapter_version: str

    def __post_init__(self):
        for name in ('requested_quantity', 'filled_quantity', 'remaining_quantity', 'fees'):
            decimal(getattr(self, name), nonnegative=True)
        if self.requested_quantity <= 0 or self.filled_quantity + self.remaining_quantity != self.requested_quantity:
            raise ValueError('execution quantity conservation violated')
        if self.status not in {'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED', 'UNKNOWN'}:
            raise ValueError('unsupported execution result')
        if self.filled_quantity > 0:
            decimal(self.average_price, positive=True)
        elif self.average_price is not None:
            raise ValueError('unfilled execution cannot have price')
        if self.status == 'FILLED' and self.remaining_quantity != 0:
            raise ValueError('FILLED must conserve full quantity')
        if self.status == 'PARTIALLY_FILLED' and not (0 < self.filled_quantity < self.requested_quantity):
            raise ValueError('partial fill quantity invalid')
        if self.funding_cash is not None:
            decimal(self.funding_cash)
        utc(self.event_time)
        utc(self.received_at)
        if self.received_at < self.event_time or not self.execution_id or not self.adapter_version:
            raise ValueError('invalid execution provenance')


@dataclass(frozen=True, slots=True)
class PositionSnapshotV1:
    account_id: str
    venue: str
    canonical_symbol: str
    mode: str
    side: str
    quantity: Decimal
    quantity_unit: str
    average_entry: Decimal | None
    equity: Decimal
    available_balance: Decimal
    margin: Decimal
    mark_price: Decimal | None
    unrealized_pnl: Decimal | None
    realized_trade_pnl: Decimal
    fees: Decimal
    funding_cash: Decimal | None
    protection_status: str
    source_order_ids: tuple[str, ...]
    source_fill_ids: tuple[str, ...]
    reconciliation_status: str
    as_of: datetime
    adapter_version: str

    def __post_init__(self):
        for name in ('quantity', 'equity', 'available_balance', 'margin', 'fees'):
            decimal(getattr(self, name), nonnegative=True)
        for name in ('realized_trade_pnl', 'unrealized_pnl', 'funding_cash'):
            value = getattr(self, name)
            if value is not None:
                decimal(value)
        if self.mode not in {'BACKTEST', 'PAPER'} or self.side not in {'LONG', 'SHORT', 'FLAT'}:
            raise ValueError('unsupported position')
        if self.quantity > 0:
            decimal(self.average_entry, positive=True)
        if (self.side == 'FLAT') != (self.quantity == 0):
            raise ValueError('position side/quantity mismatch')
        if self.mark_price is not None:
            decimal(self.mark_price, positive=True)
        utc(self.as_of)


def _decode(cls, payload: str):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate execution fields')
            out[key] = value
        return out
    obj = json.loads(payload, object_pairs_hook=unique)
    allowed = {f.name for f in fields(cls)}
    required = {f.name for f in fields(cls) if f.default is MISSING}
    if not isinstance(obj, dict) or set(obj) - allowed or not required <= set(obj):
        raise ValueError('unknown or missing execution fields')
    for f in fields(cls):
        if f.name not in obj: obj[f.name] = f.default
    for field in fields(cls):
        value = obj[field.name]
        kind = str(field.type)
        if value is None:
            continue
        if 'Decimal' in kind:
            if not isinstance(value, str):
                raise ValueError('Decimal must use canonical string')
            obj[field.name] = Decimal(value)
        elif 'datetime' in kind:
            obj[field.name] = datetime.fromisoformat(value.replace('Z', '+00:00'))
        elif kind == 'UUID':
            obj[field.name] = UUID(value)
        elif 'tuple[' in kind:
            obj[field.name] = tuple(value)
    return cls(**obj)


def intent_json(intent: ExecutionIntentV1) -> str:
    return canonical_json(intent)


def intent_from_json(payload: str) -> ExecutionIntentV1:
    return _decode(ExecutionIntentV1, payload)


def result_from_json(payload: str) -> ExecutionResultV1:
    return _decode(ExecutionResultV1, payload)


def position_from_json(payload: str) -> PositionSnapshotV1:
    return _decode(PositionSnapshotV1, payload)


class ExecutionAdapter(Protocol):
    def capabilities(self) -> frozenset[str]: ...
    def preflight(self, intent: ExecutionIntentV1, now: datetime) -> None: ...
    def submit(self, intent: ExecutionIntentV1) -> None: ...
    def cancel(self, intent_id: UUID) -> None: ...
    def amend(self, intent_id: UUID, **constraints) -> None: ...
    def events(self) -> Iterator[ExecutionResultV1]: ...
    def position_snapshot(self, canonical_symbol: str) -> PositionSnapshotV1: ...
    def reconcile(self) -> tuple[PositionSnapshotV1, ...]: ...
