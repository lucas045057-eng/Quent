from datetime import datetime, timezone
import json
import pytest

from strategies.contracts import ScreeningCandidate

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_provider_errors_redact_secrets_and_retired_hosts_are_never_called():
    from strategies.providers import PublicResearchSources
    def transport(url, params, headers):
        assert not any(host in url for host in ("gnews.io", "xoomar.com", "api.bitget.com"))
        if "coinalyze" in url:
            raise RuntimeError(headers["api_key"])
        raise AssertionError("unexpected route")
    rows = PublicResearchSources(env={
        "COINALYZE_API_KEY": "DO_NOT_PRINT",
        "GNEWS_API_KEY": "RETIRED_SECRET",
    }, transport=transport, clock=lambda: NOW).fetch(
        ScreeningCandidate(symbol="SOLUSDT", category="A", reason_codes=("DEEP",)))
    assert "DO_NOT_PRINT" not in json.dumps([r.model_dump(mode="json") for r in rows])
    assert "RETIRED_SECRET" not in json.dumps([r.model_dump(mode="json") for r in rows])
    assert not any(r.provider in {"gnews", "xoomar", "bitget_official"} for r in rows)
    assert not any(r.kind in {"EVENT_COVERAGE", "EXCHANGE_EVENT_COVERAGE", "MACRO_COVERAGE"} for r in rows)


def test_legacy_cross_oi_decoder_keeps_actual_source_timestamp():
    from quant_phase2.adapters.aggregator import normalize_cross_oi
    row = normalize_cross_oi({"code": "0", "data": [
        {"time": int(NOW.timestamp() * 1000) - 60000, "close": "100"},
        {"time": int(NOW.timestamp() * 1000), "close": "110"},
    ]}, symbol="SOLUSDT", fetched_at=NOW)
    assert row.value == __import__("decimal").Decimal("0.1") and row.source_event_time == NOW
    assert row.provider == "coinglass"


def test_spot_capability_scope_is_symbol_bound_and_restored():
    from quant_phase7.spot import spot_symbol_scope, _normalize_symbol, SpotAdapterError
    with pytest.raises(SpotAdapterError):
        _normalize_symbol("SOLUSDT")
    with spot_symbol_scope({"SOLUSDT"}, source_ref="bitget:spot:instruments"):
        assert _normalize_symbol("SOLUSDT") == "SOLUSDT"
        with pytest.raises(SpotAdapterError):
            _normalize_symbol("DOGEUSDT")
    with pytest.raises(SpotAdapterError):
        _normalize_symbol("SOLUSDT")


def test_available_but_missing_scalar_cannot_confirm():
    from strategies.contracts import MarketObservation, EvidenceNode
    o = MarketObservation(symbol="SOLUSDT", kind="SPOT_FLOW", provider="bitget", source_ref="spot",
        source_group="SPOT_FLOW", observed_at=NOW, availability="AVAILABLE", freshness="FRESH",
        quality="VALID", coverage="COMPLETE")
    assert not o.usable
    with pytest.raises(ValueError):
        EvidenceNode(observation=o, role="NEUTRAL", interpretation="no direction")


def test_spot_metadata_without_futures_type_is_parsed():
    from quant_phase1.adapters.bitget_v3.parsers import parse_instruments_response
    values = parse_instruments_response({"code": "00000", "data": [{
        "symbol": "SOLUSDT", "category": "SPOT", "baseCoin": "SOL", "quoteCoin": "USDT",
        "symbolType": "crypto", "status": "online", "pricePrecision": "2", "quantityPrecision": "3",
        "minOrderQty": "0.001", "maxOrderQty": "0",
    }]}, fetched_at=NOW, allow_documented_spot=True)
    assert values[0].symbol_type == "SPOT"


def test_price_refresh_cannot_trust_false_fresh_label():
    from strategies.refresh import refresh_candidate
    from strategies.contracts import AnalysisRequest, MarketObservation
    from datetime import timedelta
    request = AnalysisRequest(candidate=ScreeningCandidate(symbol="SOLUSDT", category="A", reason_codes=("DEEP",)),
        requested_at=NOW, deadline=NOW + timedelta(seconds=60), screening_digest="a" * 64,
        required_kinds=("PRICE",))
    class Source:
        data_source = "SYNTHETIC_FIXTURE"
        def refresh(self, request):
            return (MarketObservation(symbol="SOLUSDT", kind="PRICE", provider="bitget", source_ref="p",
                source_group="PRICE", value="100", unit="USDT", observed_at=NOW - timedelta(minutes=10),
                fetched_at=NOW, availability="AVAILABLE", freshness="FRESH", quality="VALID", coverage="COMPLETE"),)
    snapshot = refresh_candidate(request, source=Source(), now=NOW)
    assert snapshot.refresh_status == "UNAVAILABLE" and snapshot.observations[0].freshness == "STALE"


def test_free_cross_market_provider_is_explicitly_enabled_and_keeps_receipts_symbol_bound(tmp_path):
    from strategies.providers import PublicResearchSources, receipt_observation
    config = tmp_path / "services.json"
    config.write_text(json.dumps({
        "schema_version": "RESEARCH_SERVICES_V2",
        "coinalyze": {"enabled": False},
        "gnews": {"enabled": True},
        "xoomar": {"enabled": True},
        "public_cross_market": {"enabled": True, "network_transport": "native"},
    }))
    calls = []
    def transport(url, params, headers):
        calls.append((url, params))
        return {}
    rows = PublicResearchSources(env={"QUANT_V2_SERVICES_CONFIG_PATH": str(config)},
        transport=transport, clock=lambda: NOW).fetch(
            ScreeningCandidate(symbol="ETHUSDT", category="A", reason_codes=("DEEP",)))
    public = [row for row in rows if row.provider == "public_cross_market"]
    assert {row.kind for row in public} == {"CROSS_OI", "CROSS_FUNDING"}
    assert len(calls) == 7
    assert all(params.get("symbol") == "ETHUSDT" for _url, params in calls)
    assert all(host in url for (url, _params), host in zip(calls, (
        "fapi.binance.com", "fapi.binance.com", "fapi.binance.com",
        "api.bybit.com", "api.bybit.com", "api.bybit.com", "api.bybit.com")))
    fact = receipt_observation(next(row for row in public if row.kind == "CROSS_OI"))
    assert not fact.usable and fact.reason == "PUBLIC_CROSS_OI_SCOPE_UNIT_OR_HISTORY_INCOMPLETE"


def test_free_cross_market_provider_does_not_run_without_local_opt_in():
    from strategies.providers import PublicResearchSources
    calls = []
    rows = PublicResearchSources(env={}, transport=lambda *args: calls.append(args), clock=lambda: NOW).fetch(
        ScreeningCandidate(symbol="ETHUSDT", category="A", reason_codes=("DEEP",)))
    assert all("binance.com" not in call[0] and "bybit.com" not in call[0] for call in calls)
    assert not any(row.provider == "public_cross_market" for row in rows)