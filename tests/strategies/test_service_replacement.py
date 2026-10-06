from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json

from strategies.contracts import ScreeningCandidate
from strategies.providers import PublicResearchSources, receipt_observation


NOW = datetime(2026, 10, 3, 10, 30, tzinfo=timezone.utc)
A = ScreeningCandidate(symbol="SOLUSDT", category="A", reason_codes=("DEEP",))


def transport_fixture(calls):
    # SYNTHETIC_FIXTURE: no response is evidence of a live connection.
    def transport(url, params, headers):
        calls.append((url, params, headers))
        if url.endswith("/exchanges"):
            return [{"code": "A", "name": "Binance"}, {"code": "6", "name": "Bybit"}, {"code": "K", "name": "Bitget"}]
        if url.endswith("/future-markets"):
            return [dict(symbol="SOLUSDT_PERP." + e, exchange=e, symbol_on_exchange="SOLUSDT",
                base_asset="SOL", quote_asset="USDT", is_perpetual=True, margined="STABLE")
                for e in ("K", "A", "6")]
        if url.endswith("/open-interest-history"):
            return [dict(symbol=s, history=[
                {"t": int((NOW - timedelta(hours=2, minutes=30)).timestamp()), "c": 100},
                {"t": int((NOW - timedelta(hours=1, minutes=30)).timestamp()), "c": 110},
                {"t": int((NOW - timedelta(minutes=30)).timestamp()), "c": 9999},
            ]) for s in params["symbols"].split(",")]
        if url.endswith("/funding-rate"):
            return [dict(symbol=s, value=.01, update=int(NOW.timestamp() * 1000)) for s in params["symbols"].split(",")]
        if url.endswith("/liquidation-history"):
            return [dict(symbol=s, history=[]) for s in params["symbols"].split(",")]
        raise AssertionError("unexpected provider route: " + url)
    return transport


def test_active_derivatives_provider_routes_and_market_resolution():
    calls = []
    rows = PublicResearchSources(env={
        "COINALYZE_API_KEY": "SYNTHETIC_ONE",
        "GNEWS_API_KEY": "SYNTHETIC_RETIRED_KEY",
    }, transport=transport_fixture(calls), clock=lambda: NOW).fetch(A)
    assert {r.provider for r in rows} == {"coinalyze"}
    assert all(not any(old in url for old in ("gnews.io", "xoomar.com", "api.bitget.com"))
        for url, _, _ in calls)
    oi_call = next(c for c in calls if c[0].endswith("/open-interest-history"))
    assert set(oi_call[1]["symbols"].split(",")) == {"SOLUSDT_PERP.A", "SOLUSDT_PERP.6"}
    assert oi_call[1]["convert_to_usd"] == "true" and oi_call[2] == {"api_key": "SYNTHETIC_ONE"}
    oi = next(r for r in rows if r.kind == "CROSS_OI")
    fact = receipt_observation(oi)
    assert fact.provider == "coinalyze" and fact.value == D(".1") and fact.usable
    assert fact.source_event_time == NOW - timedelta(minutes=30)
    assert oi.market_symbols == ("SOLUSDT_PERP.6", "SOLUSDT_PERP.A")
    assert not receipt_observation(next(r for r in rows if r.kind == "CROSS_FUNDING")).usable
    assert next(r for r in rows if r.kind == "UNLOCK").reason == "SOURCE_CAPABILITY_UNSUPPORTED"


def test_legacy_services_sections_are_ignored_without_affecting_active_sources(tmp_path):
    from strategies.service_config import load_service_config
    path = tmp_path / "services.json"
    path.write_text(json.dumps({
        "schema_version": "RESEARCH_SERVICES_V2",
        "coinalyze": {"api_key": "SYNTHETIC_ONE"},
        "gnews": {"api_key": "SYNTHETIC_RETIRED_KEY", "enabled": True},
        "xoomar": {"enabled": True},
        "events": {"enabled": True, "state_directory": str(tmp_path / "retired-state")},
    }))
    config = load_service_config(path)
    assert "SYNTHETIC_ONE" not in repr(config)
    assert "SYNTHETIC_RETIRED_KEY" not in repr(config)
    assert not hasattr(config, "gnews") and not hasattr(config, "xoomar") and not hasattr(config, "events")
    calls = []
    rows = PublicResearchSources(env={"QUANT_V2_SERVICES_CONFIG_PATH": str(path)},
        transport=transport_fixture(calls), clock=lambda: NOW).fetch(A)
    assert next(r for r in rows if r.kind == "CROSS_OI").availability == "AVAILABLE"
    assert all(not any(old in url for old in ("gnews.io", "xoomar.com", "api.bitget.com"))
        for url, _, _ in calls)
    assert not (tmp_path / "retired-state").exists()


def test_invalid_services_file_blocks_network_and_does_not_fallback(tmp_path):
    path = tmp_path / "services.json"
    path.write_text("{")
    def forbidden(*a):
        raise AssertionError("invalid config must block calls")
    rows = PublicResearchSources(env={
        "QUANT_V2_SERVICES_CONFIG_PATH": str(path),
        "COINALYZE_API_KEY": "DO_NOT_PRINT",
    }, transport=forbidden, clock=lambda: NOW).fetch(A)
    assert rows and all(r.availability == "ERROR" for r in rows)
    assert all(r.reason == "RESEARCH_SERVICES_CONFIG_INVALID" for r in rows)
    assert "DO_NOT_PRINT" not in json.dumps([r.model_dump(mode="json") for r in rows])


def test_local_credentials_are_excluded_from_container_context():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assert "config/research_services.local.json" in (root / ".dockerignore").read_text()


def test_mixed_future_funding_clock_is_rejected():
    calls = []
    rows = PublicResearchSources(env={"COINALYZE_API_KEY": "SYNTHETIC_ONE"},
        transport=transport_fixture(calls), clock=lambda: NOW).fetch(A)
    receipt = next(r for r in rows if r.kind == "CROSS_FUNDING")
    data = json.loads(receipt.payload_json)
    data[0]["update"] = int((NOW + timedelta(hours=1)).timestamp() * 1000)
    fact = receipt_observation(receipt.model_copy(update={"payload_json": json.dumps(data)}))
    assert fact.availability == "UNAVAILABLE" and fact.value is None


def test_large_catalog_has_a_longer_bounded_timeout_without_extending_candidate_deadline(monkeypatch):
    import strategies.providers as module
    from urllib.parse import urlparse, parse_qs
    calls = []
    fixtures = transport_fixture([])
    class Response:
        def __init__(self, url, data):
            self.url = url
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return json.dumps(self.data).encode()
    class Opener:
        def open(self, request, timeout):
            parsed = urlparse(request.full_url)
            calls.append((parsed.path, timeout))
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            data = fixtures(parsed.scheme + "://" + parsed.netloc + parsed.path, params, {})
            return Response(request.full_url, data)
    monkeypatch.setattr(module, "build_opener", lambda *a: Opener())
    source = module.PublicResearchSources(env={"COINALYZE_API_KEY": "SYNTHETIC_CATALOG"}, clock=lambda: NOW)
    source.fetch(A)
    assert next(t for path, t in calls if path.endswith("/future-markets")) == 10
    calls.clear()
    module.PublicResearchSources._catalog_cache.clear()
    source.fetch(A, deadline=NOW + timedelta(seconds=5))
    assert 0 < next(t for path, t in calls if path.endswith("/future-markets")) < 5