"""Deterministic Python gate for conservative, explicitly protected exposure."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from quant_phase9.canonical import canonical_sha256
from quant_data_layer.freshness import FRESHNESS_POLICY, bitget_price_source_clock_skew_tolerance
from quant_execution.contracts import (
    AccountSnapshotV1, DecisionEnvelopeV1, ExecutionIntentV1, InstrumentSpecV1,
    QuoteV1, decimal, make_intent, utc,
)


class RiskRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RiskPolicyV1:
    version: str
    max_notional: Decimal
    max_margin: Decimal
    max_leverage: Decimal
    max_risk: Decimal
    max_exposure: Decimal
    max_reserved_risk: Decimal
    max_open_intents: int
    quote_max_age_seconds: int
    account_max_age_seconds: int
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    intent_ttl_seconds: int
    supported_decision_versions: tuple[str, ...]
    supported_code_versions: tuple[str, ...]
    supported_aux_versions: tuple[tuple[str, str], ...] = (
        ('pattern_policy_version', '1.0.0'), ('freshness_policy_version', '1.0.0'),
        ('ttl_policy_version', '1.0.0'), ('evidence_schema_version', 'PHASE9_EVIDENCE_CHAIN_V1'),
        ('prompt_version', 'NONE'),
    )

    risk_config_digest: str | None = None
    max_open_positions: int = 1
    cooldown_seconds: int = 0
    allow_pyramiding: bool = False
    allow_averaging_down: bool = False

    def __post_init__(self):
        for name in ('max_notional', 'max_margin', 'max_leverage', 'max_risk',
                     'max_exposure', 'max_reserved_risk'):
            decimal(getattr(self, name), positive=True)
        for name in ('max_spread_bps', 'max_slippage_bps'):
            decimal(getattr(self, name), nonnegative=True)
        for name in ('max_open_intents', 'quote_max_age_seconds', 'account_max_age_seconds', 'intent_ttl_seconds'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError('risk limit must be positive integer')
        if not self.version or not self.supported_decision_versions or not self.supported_code_versions:
            raise ValueError('explicit risk versions required')


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RiskRejected(code)


def approve_intent(*, envelope: DecisionEnvelopeV1, instrument: InstrumentSpecV1,
        quote: QuoteV1, account: AccountSnapshotV1, policy: RiskPolicyV1,
        stop_price: Decimal, now: datetime, trade_plan=None) -> ExecutionIntentV1:
    utc(now)
    decimal(stop_price, positive=True)
    candidate = envelope.candidate
    extra = {}
    v2 = candidate.decision_policy_version=="2.0.0"
    if v2:
        from .trade_plan import PaperTradePlanV2
        _require(isinstance(trade_plan,PaperTradePlanV2),"V2_PLAN_REQUIRED")
        _require(trade_plan.profile=="QUANT_PAPER_V2" and trade_plan.schema_version=="PAPER_TRADE_PLAN_V2"
            and envelope.thesis_digest==trade_plan.thesis_digest
            and envelope.analysis_snapshot_digest==trade_plan.snapshot_digest
            and envelope.risk_config_digest==trade_plan.risk_config_digest==policy.risk_config_digest
            and trade_plan.symbol==candidate.symbol and trade_plan.canonical_symbol==instrument.canonical_symbol
            and candidate.matched_pattern=="MARKET_EVIDENCE_CHAIN" and not candidate.conflicting_evidence_ids
            and trade_plan.side==('LONG' if candidate.direction_bias=='BULLISH' else 'SHORT')
            and trade_plan.stop_price==stop_price and trade_plan.valid_until>now
            and trade_plan.reference_price==(quote.ask if trade_plan.side=='LONG' else quote.bid)
            and trade_plan.quote_as_of==quote.as_of, "V2_PLAN_BINDING_INVALID")
        extra=dict(strategy_profile=trade_plan.profile,target_price=trade_plan.target_price,
            max_hold_seconds=trade_plan.max_hold_seconds,setup_id=trade_plan.setup_id,setup_trigger_at=trade_plan.setup_trigger_at,
            thesis_digest=trade_plan.thesis_digest,analysis_snapshot_digest=trade_plan.snapshot_digest,
            risk_config_digest=trade_plan.risk_config_digest,horizon=trade_plan.horizon,trade_plan_digest=trade_plan.digest)
    elif trade_plan is not None:
        from .paper_v1 import PaperTradePlanV1
        from quant_phase9.paper_v1 import PROFILE
        _require(isinstance(trade_plan, PaperTradePlanV1) and trade_plan.profile == PROFILE.name
            and trade_plan.snapshot_hash == envelope.evaluation_snapshot_hash
            and trade_plan.stop_price == stop_price and trade_plan.net_R >= PROFILE.min_net_R
            and candidate.confidence_band == 'HIGH' and candidate.matched_pattern == 'BREAKOUT_CONFIRMATION'
            and candidate.timeframe == '15m' and candidate.symbol in {'BTCUSDT','ETHUSDT'}
            and trade_plan.side == ('LONG' if candidate.direction_bias == 'BULLISH' else 'SHORT'), 'PAPER_V1_PLAN_INVALID')
        _require(account.exposure == 0 and account.open_intents == 0, 'PAPER_V1_POSITION_EXISTS')
        _require(account.equity > 0, 'PAPER_EQUITY_REQUIRED')
        notional = account.equity * PROFILE.max_position_notional_equity_ratio
        risk = account.equity * PROFILE.risk_per_trade_equity_ratio
        policy = replace(policy, max_notional=min(policy.max_notional,notional),
            max_margin=min(policy.max_margin,notional),max_exposure=min(policy.max_exposure,notional),
            max_leverage=min(policy.max_leverage,Decimal(1)),max_risk=min(policy.max_risk,risk),
            max_reserved_risk=min(policy.max_reserved_risk,risk),max_open_intents=1,
            max_spread_bps=min(policy.max_spread_bps,PROFILE.max_spread_bps),
            max_slippage_bps=min(policy.max_slippage_bps,PROFILE.max_slippage_bps),
            intent_ttl_seconds=min(policy.intent_ttl_seconds,PROFILE.execution_intent_ttl_seconds))
        extra = dict(strategy_profile=PROFILE.name,target_price=trade_plan.target_price,
            max_hold_seconds=PROFILE.max_hold_seconds,setup_id=trade_plan.setup_id,setup_trigger_at=trade_plan.setup_trigger_at)
    _require(envelope.effective_status == 'ACTIVE', 'INACTIVE')
    _require(candidate.eligible is True and not candidate.veto_reasons
        and candidate.pattern_status == 'MATCHED'
        and candidate.direction_bias in {'BULLISH', 'BEARISH'}, 'INELIGIBLE')
    _require(candidate.created_at <= now, 'FUTURE_DECISION')
    _require(candidate.valid_until > now, 'EXPIRED')
    _require(candidate.decision_policy_version in policy.supported_decision_versions
        and candidate.code_version in policy.supported_code_versions
        and all(getattr(candidate, name) == value for name, value in policy.supported_aux_versions),
        'UNSUPPORTED_VERSION')
    _require(candidate.input_snapshot_hash == envelope.input_snapshot_hash, 'INPUT_HASH_MISMATCH')
    _require(account.mode in {'BACKTEST', 'PAPER'}, 'UNSUPPORTED_MODE')
    _require(instrument.tradable, 'INSTRUMENT_DISABLED')
    _require(candidate.market == 'USDT_PERPETUAL' and candidate.symbol == instrument.core_symbol
        and instrument.canonical_symbol == quote.canonical_symbol
        and instrument.venue == account.venue, 'BINDING_MISMATCH')
    _require(quote.status == 'AVAILABLE', 'QUOTE_UNAVAILABLE')
    execution_quote_age_limit = min(
        policy.quote_max_age_seconds, FRESHNESS_POLICY["PRICE_EXECUTION"].hard_seconds,
    )
    receipt_at=quote.received_at or quote.as_of
    source_at=quote.source_as_of or quote.as_of
    source_skew=bitget_price_source_clock_skew_tolerance('PRICE_EXECUTION',quote.exchange,quote.source)
    receipt_age=(now-receipt_at).total_seconds()
    snapshot_age=(now-quote.as_of).total_seconds()
    source_age=(now-source_at).total_seconds()
    _require(0 <= receipt_age <= execution_quote_age_limit
        and 0 <= snapshot_age <= execution_quote_age_limit
        and -source_skew <= source_age <= execution_quote_age_limit, 'STALE_QUOTE')
    _require(0 <= (now - account.as_of).total_seconds() <= policy.account_max_age_seconds, 'STALE_ACCOUNT')
    _require(account.reconciliation_status == 'RECONCILED', 'ACCOUNT_UNRESOLVED')
    _require(account.open_intents < policy.max_open_intents, 'CONCURRENCY_LIMIT')
    _require(account.reserved_risk < policy.max_reserved_risk, 'RISK_LIMIT')
    _require(account.exposure < policy.max_exposure, 'EXPOSURE_LIMIT')
    mid = (quote.bid + quote.ask) / 2
    _require((quote.ask - quote.bid) / mid * 10000 <= policy.max_spread_bps, 'SPREAD')
    side = 'LONG' if candidate.direction_bias == 'BULLISH' else 'SHORT'
    reference = quote.ask if side == 'LONG' else quote.bid
    _require((side == 'LONG' and stop_price < reference) or
        (side == 'SHORT' and stop_price > reference), 'STOP_WRONG_SIDE')
    _require(stop_price % instrument.price_tick == 0, 'STOP_TICK')
    fraction = policy.max_slippage_bps / 10000
    worst = reference * (1 + fraction if side == 'LONG' else 1 - fraction)
    _require(worst > 0, 'SLIPPAGE_BOUND')
    notional_price = reference * (1 + fraction)
    loss_per_unit = max(abs(worst - stop_price), trade_plan.risk_per_unit) if trade_plan is not None else abs(worst - stop_price)
    available_risk = min(policy.max_risk, policy.max_reserved_risk - account.reserved_risk)
    notional_cap = min(policy.max_notional, policy.max_exposure - account.exposure,
        min(policy.max_margin, account.available_balance) * policy.max_leverage)
    raw_quantity = min(available_risk / loss_per_unit, notional_cap / notional_price,
        instrument.max_quantity)
    quantity = (raw_quantity / instrument.quantity_step).to_integral_value(rounding=ROUND_DOWN) * instrument.quantity_step
    _require(quantity >= instrument.min_quantity and quantity * reference >= instrument.min_notional, 'BELOW_MINIMUM')
    notional = quantity * notional_price
    return make_intent(
        decision_id=candidate.decision_id, evaluation_id=candidate.evaluation_id,
        evaluation_snapshot_hash=envelope.evaluation_snapshot_hash,
        input_snapshot_hash=candidate.input_snapshot_hash, core_symbol=instrument.core_symbol,
        canonical_symbol=instrument.canonical_symbol, instrument_id=instrument.instrument_id,
        venue=instrument.venue, account_id=account.account_id, mode=account.mode, side=side,
        created_at=now, valid_until=min(candidate.valid_until, now + timedelta(seconds=policy.intent_ttl_seconds)),
        approved_quantity=quantity, quantity_unit='BASE', reference_price=reference,
        quote_as_of=quote.as_of, max_notional=notional, max_margin=notional / policy.max_leverage,
        max_leverage=policy.max_leverage, entry_type='MARKET', time_in_force='IOC',
        max_spread_bps=policy.max_spread_bps, max_slippage_bps=policy.max_slippage_bps,
        stop_price=stop_price, protection_required=True, risk_budget=quantity * loss_per_unit,
        settlement_currency=instrument.settlement_currency, risk_policy_version=policy.version,
        risk_policy_hash=str(canonical_sha256(policy)), decision_policy_version=candidate.decision_policy_version,
        code_version=candidate.code_version, **extra,
    )
