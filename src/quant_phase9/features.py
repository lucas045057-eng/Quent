"""Read-only adapter from frozen snapshot facts to the shared feature projector."""
from datetime import datetime, timedelta, timezone
from quant_features.core import DEFINITIONS, FeatureObservationV1, exact_decimal, project_features, as_of_features


MAPPING = {
    'PRICE_TICKER':('PRICE','last_price'), 'PRICE_OBSERVATION':('PRICE','value'),
    'CLOSED_KLINE':('PRICE','close'), 'OPEN_INTEREST':('OI','open_interest_usd'),
    'FUNDING_RATE':('FUNDING','normalized_8h_rate'),
    'TRADE_FLOW_WINDOW':('TAKER','delta_base'), 'CVD_SNAPSHOT':('CVD','value'),
    'LIQUIDATION_WINDOW':('LIQUIDATION','convertible_notional_usd'),
    'LIQUIDATION_EVENT':('LIQUIDATION','notional_usd'),
    'NEWS_EVENT':('NEWS',None),'OPTION_CONTEXT_SNAPSHOT':('OPTIONS',None),
    'ONCHAIN_FLOW_WINDOW':('ONCHAIN',None),
}


def _timestamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('shared feature source timestamp must be UTC')
    return value.astimezone(timezone.utc)


def snapshot_features(snapshot, *, canonical_symbol: str):
    observations = []
    for source in snapshot.source_projections:
        if source.source_type not in MAPPING:
            continue
        kind,key = MAPPING[source.source_type]
        payload = source.canonical_payload
        value = exact_decimal(payload.get(key)) if key else None
        if source.source_type == 'PRICE_TICKER' and payload.get('last_price') is None:
            value = exact_decimal(payload.get('lastPr'))
        if kind == 'TAKER' and exact_decimal(payload.get('unknown_trade_count')) != 0:
            value = None
        # Never reuse realized settled funding as an earlier observation.
        if kind == 'FUNDING' and payload.get('classification') == 'REALIZED' and payload.get('next_funding_time'):
            value = None
        end = _timestamp(payload.get('bar_close_timestamp') or payload.get('window_close') or payload.get('window_end') or source.event_time or source.observed_at)
        if end is None:
            continue
        start = _timestamp(payload.get('bar_open_timestamp') or payload.get('window_open') or end)
        times = tuple(value for value in (source.captured_at,source.processed_at,source.available_at) if value is not None)
        known = max(times) if times else None
        observations.append(FeatureObservationV1(kind,canonical_symbol,value,DEFINITIONS[kind][0],
            start,end,known,source.captured_at,source.processed_at,str(source.availability_status),
            str(source.freshness_status),str(source.quality_status),str(source.coverage_status or 'UNKNOWN'),
            str(payload.get('authority','UNVERIFIED')),str(payload.get('provenance',source.canonical_digest)),
            source.source_ref,str(payload.get('revision','1')),str(payload.get('pit_status','PIT_UNVERIFIED'))))
    return as_of_features(project_features(observations),snapshot.as_of)
