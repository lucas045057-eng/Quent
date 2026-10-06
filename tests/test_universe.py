from datetime import datetime, timezone
from decimal import Decimal

from quant_phase1.contracts import DataStatus, Instrument, Ticker
from quant_phase1.universe import select_universe


NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)


def _instrument(symbol: str, *, status: str = "online", contract_type: str = "perpetual", symbol_type: str = "PERPETUAL", provider_symbol_type: str = "crypto", base: str = "BTC") -> Instrument:
    return Instrument(symbol, "USDT-FUTURES", base, "USDT", symbol_type, contract_type, status, 2, 3, Decimal("0.001"), None, NOW, {"symbolType": provider_symbol_type})


def _ticker(symbol: str, turnover: int) -> Ticker:
    return Ticker(symbol, Decimal("100"), Decimal("99.9"), Decimal("100.1"), Decimal("1"), Decimal("1"), Decimal("1000"), Decimal(turnover), Decimal("100"), Decimal("100"), NOW, NOW, NOW, DataStatus.AVAILABLE, {})


def test_universe_filters_to_online_perpetual_usdt_and_sorts_turnover():
    instruments = [
        _instrument("BTCUSDT", base="BTC"),
        _instrument("ETHUSDT", base="ETH"),
        _instrument("OFFUSDT", status="offline", base="OFF"),
        _instrument("DELUSDT", contract_type="delivery", base="DEL"),
        _instrument("RWAUSDT", base="RWA"),
    ]
    tickers = [_ticker("BTCUSDT", 100), _ticker("ETHUSDT", 200), _ticker("OFFUSDT", 900), _ticker("DELUSDT", 800), _ticker("RWAUSDT", 700)]
    selected = select_universe(instruments, tickers, limit=2)
    assert [item.symbol for item in selected] == ["ETHUSDT", "BTCUSDT"]


def test_universe_rejects_un_normalized_crypto_symbol_type():
    instrument = _instrument("BTCUSDT", symbol_type="crypto")

    assert select_universe([instrument], [_ticker("BTCUSDT", 100)]) == []


def test_universe_rejects_non_crypto_bitget_perpetual_asset_class():
    instrument = _instrument("AAPLUSDT", provider_symbol_type="stock", base="AAPL")

    assert select_universe([instrument], [_ticker("AAPLUSDT", 100)]) == []
