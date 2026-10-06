"""Build a local Paper instrument from verified public Bitget specifications."""
from decimal import Decimal as D
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.identifiers import InstrumentId,Symbol
from nautilus_trader.model.objects import Currency,Price,Quantity,Money
from strategies.market_view import InstrumentMetadata

def build_public_instrument(metadata, *, costs=None):
    if not isinstance(metadata,InstrumentMetadata) or metadata.venue!="bitget" or metadata.market!="USDT-FUTURES":
        raise ValueError("PUBLIC_INSTRUMENT_SCOPE_INVALID")
    if (metadata.settlement_currency!="USDT" or not metadata.symbol.endswith("USDT")
        or any(v is None for v in (metadata.tick,metadata.lot,metadata.min_quantity,metadata.max_quantity,metadata.min_notional))
        or metadata.max_quantity<metadata.min_quantity or len(metadata.source_digest)!=64):
        raise ValueError("PUBLIC_INSTRUMENT_METADATA_INCOMPLETE")
    fee=D(0)
    if costs is not None:
        from quant_execution.paper_v1 import PaperCostsV1
        if not isinstance(costs,PaperCostsV1):raise ValueError('EXPLICIT_PAPER_COSTS_REQUIRED')
        if costs.entry_fee_rate!=costs.exit_fee_rate:
            raise ValueError('UNSUPPORTED_PAPER_FEE_SCHEDULE')
        fee=costs.entry_fee_rate
    def precision(v):return max(0,-v.normalize().as_tuple().exponent)
    pp,qp=precision(metadata.tick),precision(metadata.lot)
    currency=Currency.from_str("USDT")
    return CryptoPerpetual(instrument_id=InstrumentId.from_str(metadata.symbol+"-PERP.BITGET_PAPER"),
        raw_symbol=Symbol(metadata.symbol),base_currency=Currency.from_str(metadata.symbol[:-4]),
        quote_currency=currency,settlement_currency=currency,is_inverse=False,
        price_precision=pp,size_precision=qp,price_increment=Price(metadata.tick,pp),
        size_increment=Quantity(metadata.lot,qp),min_quantity=Quantity(metadata.min_quantity,qp),
        max_quantity=Quantity(metadata.max_quantity,qp),min_notional=Money(metadata.min_notional,currency),
        maker_fee=fee,taker_fee=fee,ts_event=0,ts_init=0,info={"source_ref":metadata.source_ref,"source_digest":metadata.source_digest,
            "acceptance_kind":"PUBLIC_METADATA_LOCAL_PAPER"})
