from datetime import datetime, timezone
from decimal import Decimal

from quant_phase2.contracts import ContractType, DataStatus, FundingObservation, OIObservation
from quant_phase2.cross_exchange import build_cross_exchange_snapshot


def _oi(exchange: str, usd: str, status: DataStatus = DataStatus.AVAILABLE) -> OIObservation:
    now = datetime.now(timezone.utc)
    return OIObservation(
        symbol="BTCUSDT", canonical_symbol="BTC-USDT-PERP", exchange=exchange, contract_type=ContractType.PERPETUAL,
        margin_asset="USDT", settle_asset="USDT", raw_open_interest=Decimal(usd), raw_unit="USD_NOTIONAL",
        open_interest_base=None, open_interest_quote=Decimal(usd) if status is DataStatus.AVAILABLE else None,
        open_interest_usd=Decimal(usd) if status is DataStatus.AVAILABLE else None, mark_price=Decimal("1"),
        normalization_method="test", exchange_timestamp=now, fetched_at=now, processed_at=now,
        status=status, source_endpoint="test", raw_payload={},
    )


def _funding(exchange: str, rate: str, status: DataStatus = DataStatus.AVAILABLE) -> FundingObservation:
    now = datetime.now(timezone.utc)
    return FundingObservation(
        symbol="BTCUSDT", canonical_symbol="BTC-USDT-PERP", exchange=exchange, contract_type=ContractType.PERPETUAL,
        funding_rate=Decimal(rate) if status is DataStatus.AVAILABLE else None, funding_interval_seconds=28800,
        normalized_8h_rate=Decimal(rate) if status is DataStatus.AVAILABLE else None,
        predicted_funding_rate=None, realized_funding_rate=None, next_funding_time=None,
        exchange_timestamp=now, fetched_at=now, processed_at=now, status=status, source_endpoint="test", raw_payload={},
    )


def test_sparse_snapshot_does_not_fill_missing_with_zero() -> None:
    snapshot = build_cross_exchange_snapshot(
        "BTC-USDT-PERP", [_oi("bybit", "100")], [_funding("bybit", "0.001")], datetime.now(timezone.utc),
        min_oi_sources=2, min_funding_sources=2,
    )
    assert snapshot.oi_total_usd == Decimal("100")
    assert snapshot.oi_exchange_count == 1
    assert snapshot.oi_weighted_funding is None
    assert snapshot.status is DataStatus.NOT_AVAILABLE
    assert "INSUFFICIENT" in (snapshot.reason or "")


def test_weighted_funding_excludes_stale_and_marks_divergence() -> None:
    snapshot = build_cross_exchange_snapshot(
        "BTC-USDT-PERP",
        [_oi("bybit", "100"), _oi("hyperliquid", "100")],
        [_funding("bybit", "0.001"), _funding("hyperliquid", "-0.001"), _funding("okx", "0", DataStatus.STALE)],
        datetime.now(timezone.utc), min_oi_sources=1, min_funding_sources=1, funding_divergence_threshold=Decimal("0.001"),
    )
    assert snapshot.oi_weighted_funding == Decimal("0")
    assert snapshot.funding_dispersion == Decimal("0.002")
    assert snapshot.reason == "SOURCE_DIVERGENCE"
