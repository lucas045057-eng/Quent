from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from quant_phase2.persistence import _jsonable


def test_phase2_migration_is_idempotent_and_separate_from_trading_tables() -> None:
    sql = (Path(__file__).parents[1] / "migrations" / "004_phase2_derivatives.sql").read_text(encoding="utf-8").lower()
    for table in ("exchange_instruments", "open_interest", "funding_rates", "cross_exchange_derivative_snapshots"):
        assert f"create table if not exists {table}" in sql
    assert "unique" in sql
    assert "timestamptz" in sql
    assert "observation_key" in sql
    assert "create table if not exists orders" not in sql
    assert "create table if not exists positions" not in sql


def test_observation_key_is_stable_for_timestampless_provider_snapshots() -> None:
    from quant_phase2.contracts import ContractType, DataStatus, OIObservation
    from quant_phase2.persistence import _observation_key

    fetched = datetime(2026, 1, 1, tzinfo=timezone.utc)
    values = dict(
        symbol="BTC", canonical_symbol="BTC-USDT-PERP", exchange="hyperliquid",
        contract_type=ContractType.PERPETUAL, margin_asset="USDC", settle_asset="USDC",
        raw_open_interest=Decimal("10"), raw_unit="BASE_ASSET", open_interest_base=Decimal("10"),
        open_interest_quote=None, open_interest_usd=Decimal("20000"), mark_price=Decimal("2000"),
        normalization_method="base_times_mark", exchange_timestamp=None, fetched_at=fetched,
        processed_at=fetched, status=DataStatus.AVAILABLE, source_endpoint="/info",
        raw_payload={"openInterest": "10", "markPx": "2000"},
    )
    left = OIObservation(**values)
    right = OIObservation(**values)

    assert _observation_key(left) == _observation_key(right)


def test_raw_payload_json_conversion_preserves_decimal_and_utc() -> None:
    value = _jsonable({"raw": Decimal("1.20"), "time": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    assert value == {"raw": "1.20", "time": "2026-01-01T00:00:00+00:00"}
