"""Backward-only feature availability; unknown and missing values stay null."""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from quant_phase9.canonical import canonical_sha256


DEFINITIONS = {
    'PRICE': ('USDT', 3600, 'closed price or exchange observation'),
    'OI': ('USD', 300, 'Phase2 normalized quote notional; no duplicate normalization'),
    'FUNDING': ('RATE', 28800, 'observed normalized8h rate; predicted and settled rates distinct'),
    'TAKER': ('BASE', 900, 'verified buy minus sell base quantity; unknown side invalidates direction'),
    'CVD': ('BASE', 900, 'Phase3 cumulative delta at known closed window'),
    'LIQUIDATION': ('USD', 900, 'observed convertible notional; complete thresholds need COMPLETE coverage'),
    'NEWS': ('COUNT', 86400, 'known published event context'),
    'OPTIONS': ('CONTEXT', 3600, 'known options context'),
    'ONCHAIN': ('CONTEXT', 3600, 'known onchain context'),
}
DEFINITION_VERSION = 'canonical-features-v1'


def exact_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value,(bool,float)) or not isinstance(value,(Decimal,int,str)):
        raise ValueError('feature numeric value must be an exact decimal')
    try:
        result = Decimal(value)
    except Exception as exc:
        raise ValueError('invalid feature numeric value') from exc
    if not result.is_finite():
        raise ValueError('feature numeric value must be finite')
    return result


@dataclass(frozen=True, slots=True)
class FeatureObservationV1:
    kind: str
    canonical_symbol: str
    value: Decimal | None
    unit: str
    window_start: datetime
    window_end: datetime
    known_at: datetime | None
    received_at: datetime | None
    processed_at: datetime | None
    status: str
    freshness: str
    quality: str
    coverage: str
    authority: str
    provenance: str
    source_ref: str
    revision: str
    pit_status: str

    def __post_init__(self):
        if self.kind not in DEFINITIONS or self.unit != DEFINITIONS[self.kind][0]:
            raise ValueError('feature definition or unit mismatch')
        if self.value is not None and (not isinstance(self.value,Decimal) or not self.value.is_finite()):
            raise ValueError('feature value must be finite Decimal or null')
        for name in ('window_start','window_end','known_at','received_at','processed_at'):
            value = getattr(self,name)
            if value is not None and (value.tzinfo is None or value.utcoffset().total_seconds() != 0):
                raise ValueError('feature time must be UTC')
        if self.window_end < self.window_start or not all((self.canonical_symbol,self.source_ref,self.provenance)):
            raise ValueError('invalid feature provenance or window')
        if self.pit_status not in {'POINT_IN_TIME','PIT_UNVERIFIED','SYNTHETIC_FIXTURE'}:
            raise ValueError('explicit PIT coverage required')
        if self.known_at is not None and any(value is not None and value > self.known_at
            for value in (self.received_at,self.processed_at)):
            raise ValueError('known_at cannot precede availability')


@dataclass(frozen=True, slots=True)
class CanonicalFeatureV1:
    observation: FeatureObservationV1
    definition_id: str
    definition_digest: str
    observation_digest: str

    def __getattr__(self, name):
        return getattr(self.observation,name)


def project_features(observations) -> tuple[CanonicalFeatureV1,...]:
    observations = tuple(observations)
    if len(observations) > 16384:
        raise ValueError('feature projector input bound exceeded')
    result = []
    for row in observations:
        definition = {'kind':row.kind,'unit':DEFINITIONS[row.kind][0],
            'max_age_seconds':DEFINITIONS[row.kind][1],'description':DEFINITIONS[row.kind][2],
            'version':DEFINITION_VERSION,'alignment':'BACKWARD_KNOWN_AT'}
        result.append(CanonicalFeatureV1(row,DEFINITION_VERSION+':'+row.kind,
            str(canonical_sha256(definition)),str(canonical_sha256(row))))
    return tuple(sorted(result,key=lambda item:(item.canonical_symbol,item.kind,item.window_end,
        item.known_at or item.window_start,item.revision,item.source_ref)))


def as_of_features(features, as_of: datetime):
    return tuple(feature for feature in features if feature.known_at is not None
        and feature.window_end <= as_of and feature.known_at <= as_of)


@dataclass(frozen=True, slots=True)
class ResearchFrame:
    as_of: datetime
    features: tuple[CanonicalFeatureV1,...]
    require_pit: bool

    def feature(self, symbol, kind):
        rows = tuple(item for item in self.features if item.canonical_symbol == symbol and item.kind == kind)
        return max(rows,key=lambda row:(row.window_end,row.known_at,row.revision,row.source_ref)) if rows else None

    def value(self, symbol, kind):
        row = self.feature(symbol,kind)
        if row is None or row.value is None:
            return None
        if row.status != 'AVAILABLE' or row.quality != 'VALID' or row.freshness != 'FRESH':
            return None
        if row.authority in {'UNKNOWN','UNVERIFIED'} or (kind == 'LIQUIDATION' and row.coverage != 'COMPLETE'):
            return None
        if self.require_pit and row.pit_status != 'POINT_IN_TIME':
            return None
        if (self.as_of-row.window_end).total_seconds() > DEFINITIONS[kind][1]:
            return None
        return row.value


def research_frame(features, as_of, *, require_pit=False):
    return ResearchFrame(as_of,as_of_features(features,as_of),require_pit)
