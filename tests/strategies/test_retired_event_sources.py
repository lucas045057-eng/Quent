"""The retired calendar/news providers stay inert, including under old config."""
import json

import pytest

from strategies.service_config import load_service_config


@pytest.mark.parametrize(
    "provider_flags",
    [
        {"gnews": True, "xoomar": False, "events": False},
        {"gnews": False, "xoomar": True, "events": False},
        {"gnews": False, "xoomar": False, "events": True},
    ],
    ids=("gnews", "xoomar", "bitget-official"),
)
def test_legacy_enabled_sources_do_not_request_or_write_state(
    tmp_path, monkeypatch, provider_flags
):
    """Old enabled=true settings are ignored and never imply a safe RISK_FLAG."""
    from strategies import providers
    from strategies.contracts import ScreeningCandidate

    state_dir = tmp_path / "event-state"
    config_path = tmp_path / "services.json"
    old_config = {
        "schema_version": "RESEARCH_SERVICES_V2",
        "coinalyze": {"enabled": False},
        "public_cross_market": {"enabled": False},
        "gnews": {
            "enabled": provider_flags["gnews"],
            "api_key": "SYNTHETIC_LEGACY_KEY",
        },
        "xoomar": {"enabled": provider_flags["xoomar"]},
        "events": {
            "enabled": provider_flags["events"],
            "state_directory": str(state_dir),
        },
        "openai": {"enabled": False},
    }
    config_path.write_text(json.dumps(old_config), encoding="utf-8")
    config = load_service_config(config_path)

    calls = []

    def transport(url, params, headers, **kwargs):
        calls.append(url)
        if "gnews.io" in url:
            return {"articles": [], "totalArticles": 0}
        if "xoomar.com" in url:
            return {"data": [], "updatedAt": "2026-10-06T00:00:00Z"}
        if "api.bitget.com" in url:
            return {"code": "00000", "data": []}
        raise AssertionError("unexpected provider request")

    monkeypatch.setattr(providers, "configured_services", lambda env: config)
    if provider_flags["events"]:
        # Exercise the same identity branch as the live public transport, but
        # route it to an in-memory fixture so the test can never reach network.
        monkeypatch.setattr(providers, "public_json_transport", transport)
    source = providers.PublicResearchSources(env={}, transport=transport)
    rows = source.fetch(
        ScreeningCandidate(symbol="BTCUSDT", category="A", reason_codes=("FIXTURE",))
    )

    assert calls == []
    assert not state_dir.exists()
    assert not any(
        row.provider in {"gnews", "xoomar", "bitget_official"}
        or row.kind in {"EVENT_COVERAGE", "EXCHANGE_EVENT_COVERAGE", "MACRO_COVERAGE"}
        for row in rows
    )
    assert not hasattr(config, "gnews")
    assert not hasattr(config, "xoomar")
    assert not hasattr(config, "events")