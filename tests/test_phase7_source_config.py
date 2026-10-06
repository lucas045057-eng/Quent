from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant_phase1.config import (
    PHASE7_BITCOIN_DEFAULT_RESPONSE_BYTES,
    PHASE7_RPC_DEFAULT_RESPONSE_BYTES,
    PHASE7_RPC_MAX_RESPONSE_BYTES,
    Settings,
)
from quant_phase1.contracts import DataStatus
from quant_phase1.health import HealthRegistry
from quant_phase1.logging import redact_secrets, sanitize_endpoint
from quant_phase7.source_config import (
    SourceConfigurationStatus,
    phase7_source_diagnostics,
    register_phase7_source_health,
)


def test_phase7_rpc_sources_are_disabled_without_defaults():
    settings = Settings.from_env({})

    assert settings.phase7_bitcoin_rpc.enabled is False
    assert settings.phase7_ethereum_rpc.enabled is False
    diagnostics = phase7_source_diagnostics(settings)
    assert diagnostics["bitcoin_rpc"]["source_status"] == "SOURCE_DISABLED"
    assert diagnostics["ethereum_rpc"]["source_status"] == "SOURCE_DISABLED"
    assert diagnostics["bitcoin_rpc"]["host"] is None


def test_rpc_response_budget_is_bounded_and_bitcoin_specific():
    defaults = Settings.from_env({})
    assert defaults.phase7_bitcoin_rpc.max_response_bytes == PHASE7_BITCOIN_DEFAULT_RESPONSE_BYTES
    assert defaults.phase7_ethereum_rpc.max_response_bytes == PHASE7_RPC_DEFAULT_RESPONSE_BYTES

    configured = Settings.from_env({"PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES": str(24 * 1024 * 1024)})
    assert configured.phase7_bitcoin_rpc.max_response_bytes == 24 * 1024 * 1024
    diagnostics = phase7_source_diagnostics(configured)
    assert diagnostics["bitcoin_rpc"]["max_response_bytes"] == 24 * 1024 * 1024
    assert diagnostics["ethereum_rpc"]["max_response_bytes"] == PHASE7_RPC_DEFAULT_RESPONSE_BYTES

    for invalid in ("0", str(PHASE7_RPC_MAX_RESPONSE_BYTES + 1), "not-an-integer"):
        with pytest.raises(ValueError, match="PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES"):
            Settings.from_env({"PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES": invalid})


def test_enabled_source_requires_endpoint():
    with pytest.raises(ValueError, match="PHASE7_BITCOIN_RPC_URL"):
        Settings.from_env({"PHASE7_BITCOIN_RPC_ENABLED": "true"})


def test_malformed_endpoint_is_rejected_without_network_access():
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        Settings.from_env({
            "PHASE7_ETHEREUM_RPC_ENABLED": "1",
            "PHASE7_ETHEREUM_RPC_URL": "ftp://rpc.example.invalid",
        })


def test_timeout_and_auth_contracts_are_validated():
    with pytest.raises(ValueError, match="TIMEOUT_SECONDS"):
        Settings.from_env({"PHASE7_BITCOIN_RPC_TIMEOUT_SECONDS": "0"})
    with pytest.raises(ValueError, match="USERNAME and .*PASSWORD"):
        Settings.from_env({
            "PHASE7_BITCOIN_RPC_ENABLED": "1",
            "PHASE7_BITCOIN_RPC_URL": "http://127.0.0.1:8332",
            "PHASE7_BITCOIN_RPC_AUTH_MODE": "basic",
            "PHASE7_BITCOIN_RPC_USERNAME": "reader",
        })
    with pytest.raises(ValueError, match="AUTH_MODE"):
        Settings.from_env({"PHASE7_ETHEREUM_RPC_AUTH_MODE": "basic"})


def test_bitcoin_api_key_header_auth_is_secret_and_requires_a_nonempty_key():
    secret = "test-secret-value"
    settings = Settings.from_env({
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_BITCOIN_RPC_AUTH_MODE": "api_key_header",
        "PHASE7_BITCOIN_RPC_API_KEY": secret,
    })

    assert settings.phase7_bitcoin_rpc.auth_mode == "API_KEY_HEADER"
    assert settings.phase7_bitcoin_rpc.api_key.get_secret_value() == secret
    assert secret not in repr(settings.phase7_bitcoin_rpc)
    assert secret not in repr(settings)
    diagnostics = phase7_source_diagnostics(settings)
    assert secret not in repr(diagnostics)
    assert diagnostics["bitcoin_rpc"]["auth_mode"] == "API_KEY_HEADER"
    registry = HealthRegistry()
    components = register_phase7_source_health(registry, settings)
    assert components["bitcoin_rpc"].details["source_status"] == "CONFIGURED"
    assert secret not in repr(registry.snapshot())

    with pytest.raises(ValueError, match="PHASE7_BITCOIN_RPC_API_KEY") as raised:
        Settings.from_env({
            "PHASE7_BITCOIN_RPC_ENABLED": "1",
            "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
            "PHASE7_BITCOIN_RPC_AUTH_MODE": "api_key_header",
        })
    assert secret not in str(raised.value)
    for empty_key in ("", "   "):
        with pytest.raises(ValueError, match="PHASE7_BITCOIN_RPC_API_KEY"):
            Settings.from_env({
                "PHASE7_BITCOIN_RPC_ENABLED": "1",
                "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
                "PHASE7_BITCOIN_RPC_AUTH_MODE": "api_key_header",
                "PHASE7_BITCOIN_RPC_API_KEY": empty_key,
            })

    with pytest.raises(ValueError) as startup_error:
        Settings.from_env({
            "PHASE7_BITCOIN_RPC_ENABLED": "1",
            "PHASE7_BITCOIN_RPC_AUTH_MODE": "api_key_header",
            "PHASE7_BITCOIN_RPC_API_KEY": secret,
        })
    assert secret not in str(startup_error.value)

    with pytest.raises(ValueError, match="AUTH_MODE"):
        Settings.from_env({
            "PHASE7_ETHEREUM_RPC_AUTH_MODE": "api_key_header",
            "PHASE7_ETHEREUM_RPC_API_KEY": secret,
        })

    with pytest.raises(ValueError, match="requires PHASE7_BITCOIN_RPC_AUTH_MODE"):
        Settings.from_env({
            "PHASE7_BITCOIN_RPC_API_KEY": secret,
        })


def test_valid_fake_endpoints_are_loaded_and_never_exposed_in_repr_or_diagnostics():
    settings = Settings.from_env({
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "http://reader:password@127.0.0.1:8332/wallet/SECRET_TOKEN",
        "PHASE7_BITCOIN_RPC_AUTH_MODE": "basic",
        "PHASE7_BITCOIN_RPC_USERNAME": "reader",
        "PHASE7_BITCOIN_RPC_PASSWORD": "password",
        "PHASE7_ETHEREUM_RPC_ENABLED": "yes",
        "PHASE7_ETHEREUM_RPC_URL": "https://rpc.example.invalid/v2/SECRET_TOKEN?api_key=SECRET_TOKEN",
        "PHASE7_ETHEREUM_RPC_AUTH_MODE": "url",
        "PHASE7_ETHEREUM_RPC_TIMEOUT_SECONDS": "4.5",
    })

    assert settings.phase7_bitcoin_rpc.endpoint_host == "127.0.0.1:8332"
    assert settings.phase7_ethereum_rpc.endpoint_host == "rpc.example.invalid"
    assert settings.phase7_ethereum_rpc.timeout_seconds == 4.5
    assert "SECRET_TOKEN" not in repr(settings)
    assert "password" not in repr(settings)
    diagnostics = phase7_source_diagnostics(settings)
    assert diagnostics["bitcoin_rpc"]["source_status"] == "CONFIGURED"
    assert diagnostics["bitcoin_rpc"]["host"] == "127.0.0.1:8332"
    assert diagnostics["ethereum_rpc"]["host"] == "rpc.example.invalid"
    assert "SECRET_TOKEN" not in repr(diagnostics)
    assert "password" not in repr(diagnostics)


@pytest.mark.parametrize("endpoint", [
    "https://example.com/SECRET_TOKEN",
    "https://example.com/v2/SECRET_TOKEN",
    "https://example.com/?api_key=SECRET_TOKEN",
    "https://user:password@example.com/rpc",
], ids=["path", "nested-path", "query-key", "basic-auth"])
def test_secret_redaction_removes_url_path_query_and_basic_auth(endpoint: str):
    message = redact_secrets(f"endpoint={endpoint}")
    assert "SECRET_TOKEN" not in message
    assert "password" not in message
    assert "example.com" in message
    assert sanitize_endpoint(endpoint) == "example.com"


def test_health_integration_reports_configuration_without_claiming_live_data():
    settings = Settings.from_env({
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/v2/SECRET_TOKEN",
        "PHASE7_ETHEREUM_RPC_ENABLED": "0",
    })
    registry = HealthRegistry()
    components = register_phase7_source_health(
        registry, settings, checked_at=datetime(2026, 9, 23, tzinfo=timezone.utc)
    )

    assert components["bitcoin_rpc"].status is DataStatus.NOT_AVAILABLE
    assert components["bitcoin_rpc"].details["source_status"] == "CONFIGURED"
    assert components["bitcoin_rpc"].details["host"] == "rpc.example.invalid"
    assert components["ethereum_rpc"].details["source_status"] == "SOURCE_DISABLED"
    assert "SECRET_TOKEN" not in repr(registry.snapshot())


def test_configuration_reload_is_explicit_and_does_not_use_process_globals():
    first = Settings.from_env({
        "PHASE7_ETHEREUM_RPC_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_URL": "https://one.example.invalid/rpc",
    })
    second = Settings.from_env({
        "PHASE7_ETHEREUM_RPC_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_URL": "https://two.example.invalid/rpc",
    })

    assert first.phase7_ethereum_rpc.endpoint_host == "one.example.invalid"
    assert second.phase7_ethereum_rpc.endpoint_host == "two.example.invalid"
