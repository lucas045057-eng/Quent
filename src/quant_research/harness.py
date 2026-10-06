"""Five predeclared hypotheses, shared cohorts and purged chronological evaluation."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from quant_features.core import ResearchFrame
from quant_phase9.canonical import canonical_sha256


KINDS = ('PRICE','OI','FUNDING','TAKER','CVD','LIQUIDATION')
HYPOTHESES = ('PRICE_ONLY','PRICE_OI','PRICE_OI_FUNDING','PRICE_OI_TAKER',
    'BREAKOUT_OI_CVD','LIQUIDATION_OI_RESET_FLOW_REVERSAL')


@dataclass(frozen=True, slots=True)
class HypothesisConfigV1:
    minimum_price_change: Decimal
    minimum_oi_change: Decimal
    max_abs_funding: Decimal
    liquidation_minimum_usd: Decimal

    def __post_init__(self):
        if any(not isinstance(value,Decimal) or not value.is_finite() or value <= 0
            for value in (self.minimum_price_change,self.minimum_oi_change,
                self.max_abs_funding,self.liquidation_minimum_usd)):
            raise ValueError('predeclared exact research thresholds required')


@dataclass(frozen=True, slots=True)
class ResearchSampleV1:
    symbol: str
    as_of: datetime
    label_end: datetime
    previous: ResearchFrame
    current: ResearchFrame
    future_return: Decimal
    settled_funding_rate: Decimal | None
    regime: str

    def __post_init__(self):
        if not self.previous.as_of < self.as_of == self.current.as_of < self.label_end:
            raise ValueError('research feature/label chronology violated')
        if not isinstance(self.future_return,Decimal) or not self.future_return.is_finite():
            raise ValueError('exact research label required')


def _eligible(row):
    return all(row.current.value(row.symbol,kind) is not None and
        row.previous.value(row.symbol,kind) is not None for kind in KINDS)


def directions(row: ResearchSampleV1, config: HypothesisConfigV1):
    if not _eligible(row):
        return {name:None for name in HYPOTHESES}
    prev = {kind:row.previous.value(row.symbol,kind) for kind in KINDS}
    cur = {kind:row.current.value(row.symbol,kind) for kind in KINDS}
    if prev['PRICE'] <= 0 or prev['OI'] <= 0:
        return {name:None for name in HYPOTHESES}
    price_change = cur['PRICE']/prev['PRICE']-1
    oi_change = cur['OI']/prev['OI']-1
    side = 1 if price_change >= config.minimum_price_change else (
        -1 if price_change <= -config.minimum_price_change else 0)
    oi_side = side if oi_change >= config.minimum_oi_change else 0
    history = tuple(value for feature in row.previous.features
        if feature.kind=='PRICE' and feature.canonical_symbol==row.symbol
        for value in (ResearchFrame(row.previous.as_of,(feature,),row.previous.require_pit).value(row.symbol,'PRICE'),)
        if value is not None and value>0)
    breakout = (1 if history and cur['PRICE']>max(history)*(1+config.minimum_price_change)
        else -1 if history and cur['PRICE']<min(history)*(1-config.minimum_price_change) else 0)
    reversal = 0
    if (cur['LIQUIDATION'] >= config.liquidation_minimum_usd
        and oi_change <= -config.minimum_oi_change and cur['TAKER']*prev['TAKER'] < 0):
        reversal = 1 if cur['TAKER'] > 0 else -1
    return {
        'PRICE_ONLY':side,'PRICE_OI':oi_side,
        'PRICE_OI_FUNDING':oi_side if abs(cur['FUNDING']) <= config.max_abs_funding else 0,
        'PRICE_OI_TAKER':oi_side if cur['TAKER']*oi_side > 0 else 0,
        'BREAKOUT_OI_CVD':breakout if oi_change>=config.minimum_oi_change
            and (cur['CVD']-prev['CVD'])*breakout>0 else 0,
        'LIQUIDATION_OI_RESET_FLOW_REVERSAL':reversal,
    }


def walk_forward(samples, *, test_size: int, min_train: int, embargo_seconds: int):
    samples = tuple(sorted(samples,key=lambda row:(row.as_of,row.symbol)))
    if not 0 < test_size <= 4096 or min_train < 1 or embargo_seconds < 0:
        raise ValueError('invalid chronological split')
    folds = []
    for start in range(min_train,len(samples),test_size):
        testing = samples[start:start+test_size]
        cutoff = testing[0].as_of-timedelta(seconds=embargo_seconds)
        training = tuple(row for row in samples[:start] if row.label_end < cutoff)
        if len(training) >= min_train:
            folds.append((training,testing))
    return tuple(folds)


def compare_hypotheses(samples, config, *, maker_bps, taker_bps, spread_bps, slippage_bps):
    for value in (maker_bps,taker_bps,spread_bps,slippage_bps):
        if not isinstance(value,Decimal) or not value.is_finite() or value < 0:
            raise ValueError('cost assumptions must be explicit finite Decimals')
    samples = tuple(samples)
    if len(samples) > 16384:
        raise ValueError('research cohort bound exceeded')
    eligible = tuple(row for row in samples if _eligible(row)
        and row.previous.value(row.symbol,'OI') > 0 and row.previous.value(row.symbol,'PRICE') > 0)
    cohort_digest = str(canonical_sha256(tuple((row.symbol,row.as_of,row.label_end) for row in eligible)))
    hypothesis_rows = {}
    for name in HYPOTHESES:
        gross,fees,carry,drag = (Decimal('0') for _ in range(4))
        unresolved = 0
        trades = 0
        for row in eligible:
            side = directions(row,config)[name]
            if not side:
                continue
            trades += 1
            gross += row.future_return*side
            fees += 2*taker_bps/10000  # Market entries and exits, both taker.
            drag += 2*(spread_bps+slippage_bps)/10000  # Conservative full spread each leg.
            if row.settled_funding_rate is None:
                unresolved += 1
            else:
                carry -= side*row.settled_funding_rate
        hypothesis_rows[name] = {'cohort_digest':cohort_digest,'trades':trades,
            'gross_return_sum':gross,'fee_return_cost':fees,'spread_slippage_return_cost':drag,
            'funding_return_cash':None if unresolved else carry,
            'net_return_sum':None if unresolved else gross-fees-drag+carry,
            'unresolved_costs':unresolved}
    return {'schema':'QUANT_RESEARCH_COMPARISON_V1','eligible_count':len(eligible),
        'excluded_count':len(samples)-len(eligible),'hypotheses':hypothesis_rows,
        'symbols':tuple(sorted({row.symbol for row in eligible})),
        'regimes':tuple(sorted({row.regime for row in eligible})),
        'cost_assumptions':{'maker_bps':maker_bps,'taker_bps':taker_bps,'spread_bps':spread_bps,
            'slippage_bps':slippage_bps,'funding':'one declared boundary per label; mark/entry ratio1',
            'liquidity':'synthetic conservative full-spread market fill'},
        'breakout_definition':'beyond min/max of usable prior closed PRICE observations within shared freshness limit',
        'fixture_only':any(feature.pit_status != 'POINT_IN_TIME' for row in eligible for feature in row.current.features),
        'production_policy_changed':False}
