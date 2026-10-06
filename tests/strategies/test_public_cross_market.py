from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from strategies.public_cross_market import normalize_public_cross_oi, normalize_public_cross_funding

NOW = datetime(2026, 10, 6, 4, 2, tzinfo=timezone.utc)

def ms(value):
    return int(value.timestamp() * 1000)

def test_cross_oi_uses_two_aligned_closed_public_venues_and_normalizes_base_units():
    earlier = datetime(2026, 10, 6, 3, 15, tzinfo=timezone.utc)
    later = earlier + timedelta(minutes=15)
    payload = {
        "binance": {"open_interest_history": [
            {"symbol": "ETHUSDT", "timestamp": ms(earlier), "sumOpenInterestValue": "1000000"},
            {"symbol": "ETHUSDT", "timestamp": ms(later), "sumOpenInterestValue": "1100000"},
        ]},
        "bybit": {
            "open_interest": {"retCode": 0, "result": {"category": "linear", "symbol": "ETHUSDT", "list": [
                {"timestamp": str(ms(later)), "openInterest": "20"},
                {"timestamp": str(ms(earlier)), "openInterest": "20"},
            ]}},
            "mark_price_symbol": "ETHUSDT",
            "mark_price_klines": {"retCode": 0, "result": {"symbol": "ETHUSDT", "list": [
                [str(ms(earlier)-60000), "99", "101", "98", "100", "0", "0"],
                [str(ms(later)-60000), "109", "111", "108", "110", "0", "0"],
            ]}},
        },
    }

    fact = normalize_public_cross_oi(payload, symbol="ETHUSDT", fetched_at=NOW)

    assert fact.usable
    assert fact.value == D("0.1")
    assert fact.unit == "CHANGE_RATIO"
    assert fact.observed_at == later
    assert fact.provider == "public_cross_market"
    assert "NOT_WHOLE_MARKET" in fact.reason


def test_cross_oi_rejects_unaligned_or_open_15m_samples():
    earlier = datetime(2026, 10, 6, 3, 15, tzinfo=timezone.utc)
    later = earlier + timedelta(minutes=15)
    payload = {
        "binance": {"open_interest_history": [
            {"symbol": "ETHUSDT", "timestamp": ms(earlier), "sumOpenInterestValue": "1000000"},
            {"symbol": "ETHUSDT", "timestamp": ms(later), "sumOpenInterestValue": "1100000"},
        ]},
        "bybit": {
            "open_interest": {"retCode": 0, "result": {"category": "linear", "symbol": "ETHUSDT", "list": [
                {"timestamp": str(ms(later)), "openInterest": "20"},
                {"timestamp": str(ms(earlier)-1), "openInterest": "20"},
            ]}},
            "mark_price_symbol": "ETHUSDT",
            "mark_price_klines": {"retCode": 0, "result": {"list": []}},
        },
    }

    fact = normalize_public_cross_oi(payload, symbol="ETHUSDT", fetched_at=NOW)

    assert not fact.usable
    assert fact.value is None
    assert fact.reason == "PUBLIC_CROSS_OI_SCOPE_UNIT_OR_HISTORY_INCOMPLETE"


def test_cross_funding_verifies_native_periods_and_selects_largest_absolute_8h_rate():
    now_ms = ms(NOW)
    interval_ms = 8 * 60 * 60 * 1000
    latest_settlement = now_ms - 60_000
    payload = {
        "binance": {
            "premium_index": {"symbol": "ETHUSDT", "lastFundingRate": "0.00002",
                "time": now_ms, "nextFundingTime": latest_settlement + interval_ms},
            "funding_history": [
                {"symbol": "ETHUSDT", "fundingTime": latest_settlement - interval_ms},
                {"symbol": "ETHUSDT", "fundingTime": latest_settlement},
            ],
        },
        "bybit": {
            "tickers": {"retCode": 0, "time": now_ms, "result": {"category": "linear", "list": [
                {"symbol": "ETHUSDT", "fundingRate": "-0.00015", "fundingIntervalHour": "8",
                    "nextFundingTime": str(latest_settlement + interval_ms)},
            ]}},
            "funding_history": {"retCode": 0, "result": {"retCode": 0, "list": [
                {"symbol": "ETHUSDT", "fundingRate": "-0.00014", "fundingRateTimestamp": str(latest_settlement - interval_ms)},
                {"symbol": "ETHUSDT", "fundingRate": "-0.00015", "fundingRateTimestamp": str(latest_settlement)},
            ]}},
            "instruments": {"retCode": 0, "result": {"category": "linear", "list": [
                {"symbol": "ETHUSDT", "fundingInterval": "480"},
            ]}},
        },
    }

    fact = normalize_public_cross_funding(payload, symbol="ETHUSDT", fetched_at=NOW)

    assert fact.usable
    assert fact.value == D("-0.00015")
    assert fact.unit == "RATE_RATIO"
    assert fact.observed_at == NOW
    assert fact.source_context["selected_provider"] == "bybit"


def test_cross_funding_fails_closed_on_unverified_period_or_foreign_symbol():
    payload = {
        "binance": {"premium_index": {"symbol": "ETHUSDT", "lastFundingRate": "0.00002",
            "time": ms(NOW), "nextFundingTime": ms(NOW)+8*60*60*1000},
            "funding_history": []},
        "bybit": {"tickers": {"retCode": 0, "time": ms(NOW), "result": {"list": [
            {"symbol": "ETHUSDT", "fundingRate": "-0.00015", "fundingIntervalHour": "8"}]}},
            "funding_history": {"retCode": 0, "result": {"retCode": 0, "list": [
                {"symbol": "ETHUSDT", "fundingRateTimestamp": str(ms(NOW)-8*60*60*1000)},
                {"symbol": "ETHUSDT", "fundingRateTimestamp": str(ms(NOW))},
            ]}},
            "instruments": {"retCode": 0, "result": {"list": [
            {"symbol": "ETHUSDT", "fundingInterval": "480"}]}}},
    }

    fact = normalize_public_cross_funding(payload, symbol="SOLUSDT", fetched_at=NOW)

    assert not fact.usable
    assert fact.value is None
    assert fact.reason == "PUBLIC_CROSS_FUNDING_SCOPE_CLOCK_OR_PERIOD_INCOMPLETE"
