"""One exact funding calculator and payment identity for backtest and local Paper."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN

from quant_execution.contracts import decimal, utc
from quant_phase9.canonical import canonical_sha256


@dataclass(frozen=True, slots=True)
class FundingObservationV1:
    canonical_symbol: str
    boundary: datetime
    rate: Decimal | None
    mark_price: Decimal | None
    currency: str
    observed_at: datetime
    known_at: datetime
    classification: str
    status: str
    quality: str
    authority: str
    source_ref: str
    provenance: str
    currency_precision: int = 8

    def __post_init__(self):
        for value in (self.boundary,self.observed_at,self.known_at):
            utc(value)
        if self.rate is not None:
            decimal(self.rate)
        if self.mark_price is not None:
            decimal(self.mark_price,positive=True)
        if self.known_at < self.observed_at or not self.source_ref or not self.provenance:
            raise ValueError('invalid funding provenance')
        if self.currency != 'USDT' or self.currency_precision != 8:
            raise ValueError('first pilot supports explicit linear USDT settlement only')


@dataclass(frozen=True, slots=True)
class FundingPositionV1:
    account_id: str
    canonical_symbol: str
    signed_quantity: Decimal
    as_of: datetime
    reconciliation_status: str
    quantity_unit: str

    def __post_init__(self):
        decimal(self.signed_quantity)
        utc(self.as_of)


@dataclass(frozen=True, slots=True)
class FundingPaymentV1:
    payment_key: str
    account_id: str
    canonical_symbol: str
    boundary: datetime
    rate: Decimal | None
    mark_price: Decimal | None
    signed_quantity: Decimal | None
    cash: Decimal | None
    currency: str
    source_ref: str
    provenance: str
    status: str
    reason: str


def calculate_funding(observation: FundingObservationV1, position: FundingPositionV1 | None,
                      *, now: datetime) -> FundingPaymentV1:
    utc(now)
    account = position.account_id if position is not None else 'UNKNOWN'
    key = str(canonical_sha256({'schema':'QUANT_FUNDING_PAYMENT_KEY_V1',
        'account':account,'instrument':observation.canonical_symbol,'boundary':observation.boundary}))
    reason = ''
    if observation.boundary > now or observation.known_at > now:
        reason = 'NOT_YET_KNOWN'
    elif observation.rate is None:
        reason = 'RATE_UNAVAILABLE'
    elif observation.mark_price is None:
        reason = 'MARK_UNAVAILABLE'
    elif observation.status != 'AVAILABLE' or observation.quality != 'VALID' or not observation.authority or observation.authority in {'UNKNOWN','UNVERIFIED'}:
        reason = 'SOURCE_UNAVAILABLE'
    elif observation.classification not in {'SETTLED','REALIZED'}:
        reason = 'UNSETTLED_RATE'
    elif position is None:
        reason = 'POSITION_UNAVAILABLE'
    elif (position.canonical_symbol != observation.canonical_symbol or position.as_of != observation.boundary
        or position.reconciliation_status != 'RECONCILED' or position.quantity_unit != 'BASE'):
        reason = 'POSITION_UNRESOLVED'
    cash = None
    if not reason:
        raw_cash = -position.signed_quantity * observation.mark_price * observation.rate
        cash = raw_cash.quantize(Decimal(10) ** -observation.currency_precision,rounding=ROUND_HALF_EVEN)
    return FundingPaymentV1(key,account,observation.canonical_symbol,observation.boundary,
        observation.rate,observation.mark_price,position.signed_quantity if position else None,
        cash,observation.currency,observation.source_ref,observation.provenance,
        'UNRESOLVED' if reason else 'RESOLVED',reason)


class FundingLedger:
    def __init__(self):
        self._payments = {}

    def add(self, payment: FundingPaymentV1) -> bool:
        if payment.status != 'RESOLVED' or payment.cash is None:
            raise ValueError('unresolved funding cannot enter cash ledger')
        existing = self._payments.get(payment.payment_key)
        if existing is not None:
            if existing != payment:
                raise ValueError('immutable funding payment conflict')
            return False
        self._payments[payment.payment_key] = payment
        return True

    @property
    def total_cash(self):
        return sum((payment.cash for payment in self._payments.values()),Decimal('0'))

    @property
    def payments(self):
        return tuple(self._payments[key] for key in sorted(self._payments))
