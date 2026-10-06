from datetime import datetime, timezone
from decimal import Decimal
import asyncio

import pytest

from quant_phase2.adapters.base import AdapterSchemaError
from quant_phase2.adapters.base import PublicHTTPAdapter
from quant_phase2.adapters.bitget import BitgetUTAAdapter
from quant_phase2.adapters.bybit import BybitV5Adapter
from quant_phase2.adapters.hyperliquid import HyperliquidAdapter
from quant_phase2.contracts import DataStatus


def now() -> datetime:
    return datetime.now(timezone.utc)


def test_bitget_ticker_preserves_raw_oi_when_unit_is_not_documented() -> None:
    oi, funding = BitgetUTAAdapter().parse_tickers(
        {"code": "00000", "data": [{
            "symbol": "BTCUSDT", "ts": "1700000000000", "openInterest": "100",
            "markPrice": "2000", "fundingRate": "0.0001", "nextFundingTime": "1700001000000",
        }]},
        now(),
    )
    assert oi[0].raw_open_interest == Decimal("100")
    assert oi[0].raw_unit == "UNCONFIRMED"
    assert oi[0].normalization_method == "UNCONFIRMED_UNIT"
    assert oi[0].open_interest_usd is None
    assert oi[0].status is DataStatus.AVAILABLE
    assert funding[0].funding_rate == Decimal("0.0001")


def test_bitget_v3_current_funding_uses_interval_field() -> None:
    funding = BitgetUTAAdapter().parse_current_funding(
        {"code": "00000", "data": [{
            "symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateInterval": "8H",
            "nextUpdate": "1700001000000"
        }]},
        now(), symbol="BTCUSDT",
    )
    assert funding.funding_interval_seconds == 28800
    assert funding.normalized_8h_rate == Decimal("0.0001")


def test_bitget_v3_current_funding_numeric_interval_is_hours() -> None:
    funding = BitgetUTAAdapter().parse_current_funding(
        {"code": "00000", "data": [{
            "symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateInterval": "8",
            "nextUpdate": "1700001000000"
        }]},
        now(), symbol="BTCUSDT",
    )

    assert funding.funding_interval_seconds == 28800
    assert funding.normalized_8h_rate == Decimal("0.0001")


def test_bitget_current_funding_clock_bound_only_to_matching_live_rate():
    from dataclasses import replace
    from datetime import timedelta
    at=now()
    adapter=BitgetUTAAdapter()
    _, tickers=adapter.parse_tickers({'code':'00000','data':[{'symbol':'BTCUSDT','ts':str(int(at.timestamp()*1000)),
        'fundingRate':'0.0001'}]},at)
    current=adapter.parse_current_funding({'code':'00000','data':[{'symbol':'BTCUSDT','fundingRate':'0.0001',
        'fundingRateInterval':'8','nextUpdate':str(int((at+timedelta(hours=1)).timestamp()*1000))}]},at,symbol='BTCUSDT')
    bound=adapter.bind_funding_clock(tickers[0],current)
    assert bound.exchange_timestamp==tickers[0].exchange_timestamp
    assert bound.normalized_8h_rate==Decimal('.0001')
    assert bound.raw_payload['clock_binding']=='EXACT_RATE_AND_SYMBOL_MATCH_WITHIN_30_SECONDS'
    for wrong in (replace(tickers[0],symbol='ETHUSDT'),replace(tickers[0],funding_rate=Decimal('.0002')),
        replace(tickers[0],exchange_timestamp=at-timedelta(seconds=31)),replace(tickers[0],exchange_timestamp=None)):
        assert adapter.bind_funding_clock(wrong,current).exchange_timestamp is None


def test_current_funding_response_cannot_claim_another_symbol():
    with pytest.raises(AdapterSchemaError):
        BitgetUTAAdapter().parse_current_funding({'code':'00000','data':{'symbol':'ETHUSDT','fundingRate':'.0001'}},now(),symbol='BTCUSDT')


def test_bitget_v3_instruments_uses_live_fund_interval_and_quantity_multiplier() -> None:
    rows = BitgetUTAAdapter.parse_instruments(
        {"code": "00000", "data": [{
            "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT",
            "type": "perpetual", "fundInterval": "8", "quantityMultiplier": "0.0001",
        }]},
        now(),
    )

    assert rows[0].funding_interval_seconds == 28800
    assert rows[0].contract_size == Decimal("0.0001")


def test_bybit_linear_ticker_normalizes_base_oi() -> None:
    oi, funding = BybitV5Adapter().parse_ticker(
        {"retCode": 0, "result": {"list": [{
            "symbol": "BTCUSDT", "markPrice": "2000", "openInterest": "100",
            "fundingRate": "0.0001", "nextFundingTime": "1700001000000",
        }]}, "time": 1700000000000},
        now(), symbol="BTCUSDT", interval_seconds=28800,
    )
    assert oi.open_interest_base == Decimal("100")
    assert oi.open_interest_usd == Decimal("200000")
    assert funding.normalized_8h_rate == Decimal("0.0001")


def test_hyperliquid_contexts_use_explicit_asset_metadata() -> None:
    meta, oi, funding = HyperliquidAdapter().parse_meta_and_contexts(
        [{"universe": [{"name": "BTC", "szDecimals": 5}]}, [{
            "openInterest": "10", "markPx": "2000", "funding": "0.0001", "oraclePx": "1999"
        }]],
        now(),
    )
    assert meta[0].exchange_symbol == "BTC"
    assert oi[0].open_interest_usd == Decimal("20000")
    assert funding[0].funding_interval_seconds == 3600
    assert funding[0].normalized_8h_rate == Decimal("0.0008")


def test_bybit_bad_schema_is_not_silently_accepted() -> None:
    with pytest.raises(AdapterSchemaError):
        BybitV5Adapter().parse_ticker({"retCode": 0, "result": {"list": []}}, now(), symbol="BTCUSDT", interval_seconds=28800)


def test_public_http_adapter_retries_429_with_retry_after(monkeypatch) -> None:
    class Response:
        def __init__(self, status: int):
            self.status = status
            self.headers = {"Retry-After": "0"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {"ok": True}

    class Session:
        def __init__(self):
            self.statuses = iter((429, 200))

        def get(self, *_args, **_kwargs):
            return Response(next(self.statuses))

    sleeps = []

    async def fake_sleep(value):
        sleeps.append(value)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    adapter = PublicHTTPAdapter("https://example.test", session=Session())
    payload = asyncio.run(adapter.get_json("/public"))

    assert payload == {"ok": True}
    assert sleeps == [0.0]


def test_public_http_adapter_rate_limit_scheduler_is_per_adapter(monkeypatch) -> None:
    sleeps = []

    async def fake_sleep(value):
        sleeps.append(value)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    adapter = PublicHTTPAdapter("https://example.test", rate_limit_per_second=10, jitter_seconds=0)
    asyncio.run(adapter._wait_for_request_slot())
    asyncio.run(adapter._wait_for_request_slot())

    assert sleeps == [pytest.approx(0.1, abs=0.01)]
