import pytest

from quant_phase1.config import Settings


def test_phase4_defaults_keep_public_collection_bounded_and_disabled():
    settings = Settings.from_env({})

    assert settings.phase4_enabled is False
    assert settings.phase4_liquidation_event_retention_hours == 24
    assert settings.phase4_liquidation_retention_days == {
        "1m": 7,
        "5m": 30,
        "15m": 90,
        "1H": 180,
        "4H": 365,
    }
    assert settings.phase4_long_short_retention_days == 90
    assert settings.phase4_basis_retention_days == 90
    assert settings.phase4_cross_exchange_retention_days == 90
    assert settings.phase4_enrichment_retention_days == 90
    assert 0 < settings.phase4_max_basis_timestamp_skew_seconds <= 3_600
    assert 0 < settings.phase4_queue_capacity <= 10_000
    assert settings.phase4_event_budget == 20_000
    assert settings.phase4_event_bytes_budget == 32 * 1024 * 1024
    assert 1.0 <= settings.phase4_rest_cycle_seconds <= 3_600.0


def test_phase4_uses_official_public_endpoints():
    settings = Settings.from_env({})

    assert settings.phase4_bitget_classic_rest_base_url == "https://api.bitget.com"
    assert settings.phase4_bitget_uta_rest_base_url == "https://api.bitget.com"
    assert settings.phase4_bitget_uta_ws_public_url == "wss://ws.bitget.com/v3/ws/public"
    assert settings.phase4_bybit_rest_base_url == "https://api.bybit.com"
    assert settings.phase4_bybit_ws_public_linear_url == "wss://stream.bybit.com/v5/public/linear"
    assert settings.phase4_hyperliquid_info_url == "https://api.hyperliquid.xyz/info"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PHASE4_LIQUIDATION_EVENT_RETENTION_HOURS", "0"),
        ("PHASE4_LIQUIDATION_1M_RETENTION_DAYS", "0"),
        ("PHASE4_LONG_SHORT_RETENTION_DAYS", "0"),
        ("PHASE4_MAX_BASIS_TIMESTAMP_SKEW_SECONDS", "0"),
        ("PHASE4_QUEUE_CAPACITY", "10001"),
        ("PHASE4_EVENT_BUDGET", "100001"),
        ("PHASE4_EVENT_BYTES_BUDGET", str(64 * 1024 * 1024 + 1)),
        ("PHASE4_REST_CYCLE_SECONDS", "0"),
    ],
)
def test_phase4_rejects_invalid_bounded_configuration(name, value):
    with pytest.raises(ValueError, match=name):
        Settings.from_env({name: value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PHASE4_LIQUIDATION_EVENT_RETENTION_HOURS", "169"),
        ("PHASE4_LIQUIDATION_1M_RETENTION_DAYS", "366"),
        ("PHASE4_LIQUIDATION_5M_RETENTION_DAYS", "366"),
        ("PHASE4_LIQUIDATION_15M_RETENTION_DAYS", "366"),
        ("PHASE4_LIQUIDATION_1H_RETENTION_DAYS", "366"),
        ("PHASE4_LIQUIDATION_4H_RETENTION_DAYS", "366"),
        ("PHASE4_LONG_SHORT_RETENTION_DAYS", "366"),
        ("PHASE4_BASIS_RETENTION_DAYS", "366"),
        ("PHASE4_CROSS_EXCHANGE_RETENTION_DAYS", "366"),
        ("PHASE4_ENRICHMENT_RETENTION_DAYS", "366"),
        ("PHASE4_MAX_BASIS_TIMESTAMP_SKEW_SECONDS", "3601"),
    ],
)
def test_phase4_rejects_retention_and_skew_values_above_safe_limits(name, value):
    with pytest.raises(ValueError, match=name):
        Settings.from_env({name: value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), ("1", True), ("false", False), ("true", True), ("no", False), ("yes", True)],
)
def test_phase4_enabled_accepts_only_documented_boolean_values(value, expected):
    assert Settings.from_env({"PHASE4_ENABLED": value}).phase4_enabled is expected


@pytest.mark.parametrize("value", ["ture", "2", "enabled"])
def test_phase4_enabled_rejects_invalid_nonempty_boolean_values(value):
    with pytest.raises(ValueError, match="PHASE4_ENABLED"):
        Settings.from_env({"PHASE4_ENABLED": value})


@pytest.mark.parametrize("value", ["0.9", "3600.1"])
def test_phase4_rejects_rest_cycle_outside_inclusive_bounds(value):
    with pytest.raises(ValueError, match="PHASE4_REST_CYCLE_SECONDS"):
        Settings.from_env({"PHASE4_REST_CYCLE_SECONDS": value})


@pytest.mark.parametrize("value", ["1.0", "3600.0"])
def test_phase4_accepts_rest_cycle_at_inclusive_bounds(value):
    assert Settings.from_env({"PHASE4_REST_CYCLE_SECONDS": value}).phase4_rest_cycle_seconds == float(value)


def test_phase4_keeps_paper_guard_and_private_credential_rejection():
    with pytest.raises(ValueError, match="TRADING_MODE=paper"):
        Settings.from_env({"TRADING_MODE": "live", "PHASE4_ENABLED": "1"})

    with pytest.raises(ValueError, match="private API credentials"):
        Settings.from_env({"BITGET_API_SECRET": "private", "PHASE4_ENABLED": "1"})
