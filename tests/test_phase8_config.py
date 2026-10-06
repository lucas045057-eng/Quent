import pytest

from quant_phase8 import config


Phase8Settings = getattr(config, "Phase8Settings", None)


def test_phase8_defaults_are_disabled_bounded_and_paper_only():
    assert Phase8Settings is not None, "Phase8Settings must expose the Phase 8 configuration contract"
    settings = Phase8Settings.from_env({})

    assert settings.enabled is False
    assert settings.trading_mode == "paper"
    assert settings.expiry_days == 90
    assert settings.max_abs_moneyness_pct == 5
    assert settings.max_ticker_instruments_per_currency == 64
    assert settings.max_ticker_subscriptions_total == 128
    assert settings.max_full_chain_records_per_underlying == 2048
    assert settings.max_full_chain_records_total == 4096
    assert settings.max_markprice_channels_total == 4
    assert settings.chain_snapshot_interval_seconds == 3600
    assert settings.markprice_snapshot_interval_seconds == 900
    assert settings.context_interval_seconds == 900
    assert settings.chain_stale_after_seconds == 5400
    assert settings.ticker_stale_after_seconds == 300
    assert settings.markprice_stale_after_seconds == 900
    assert settings.rest_max_response_bytes == 8 * 1024 * 1024
    assert settings.ws_max_message_bytes == 1024 * 1024
    assert settings.ws_max_queued_bytes == 16 * 1024 * 1024
    assert settings.ws_subscribe_min_interval_seconds == 0.35
    assert settings.ws_subscribe_batch_size == 32
    assert settings.snapshot_retention_days == 7
    assert settings.context_retention_days == 30
    assert settings.lifecycle_retention_days == 90
    assert settings.retention_enforcement is False


@pytest.mark.parametrize(
    ("environment", "setting_name"),
    [
        ({"PHASE8_OPTIONS_ENABLED": "sometimes"}, "PHASE8_OPTIONS_ENABLED"),
        ({"PHASE8_OPTIONS_MAX_TICKER_INSTRUMENTS_PER_CURRENCY": "0"}, "MAX_TICKER"),
        ({"PHASE8_OPTIONS_MAX_TICKER_INSTRUMENTS_PER_CURRENCY": "129"}, "MAX_TICKER"),
        ({"PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL": "32"}, "MAX_TICKER"),
        ({"PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING": "4097"}, "FULL_CHAIN"),
        ({"PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL": "1024"}, "FULL_CHAIN"),
        ({"PHASE8_OPTIONS_MAX_MARKPRICE_CHANNELS_TOTAL": "0"}, "MARKPRICE_CHANNELS"),
        ({"PHASE8_OPTIONS_MAX_REST_RESPONSE_BYTES": str(32 * 1024 * 1024 + 1)}, "MAX_REST_RESPONSE_BYTES"),
        ({"PHASE8_OPTIONS_WS_MAX_MESSAGE_BYTES": str(4 * 1024 * 1024 + 1)}, "WS_MAX_MESSAGE_BYTES"),
        ({"PHASE8_OPTIONS_WS_MAX_QUEUED_BYTES": "100"}, "WS_MAX_QUEUED_BYTES"),
        ({"PHASE8_OPTIONS_WS_SUBSCRIBE_MIN_INTERVAL_SECONDS": "0.1"}, "WS_SUBSCRIBE_MIN_INTERVAL_SECONDS"),
        ({"PHASE8_OPTIONS_WS_SUBSCRIBE_BATCH_SIZE": "33"}, "WS_SUBSCRIBE_BATCH_SIZE"),
        ({"PHASE8_OPTIONS_SNAPSHOT_RETENTION_DAYS": "0"}, "SNAPSHOT_RETENTION_DAYS"),
        ({"PHASE8_OPTIONS_RETENTION_ENFORCEMENT": "enabled"}, "RETENTION_ENFORCEMENT"),
        ({"TRADING_MODE": "live"}, "TRADING_MODE=paper"),
    ],
)
def test_invalid_phase8_configuration_fails_without_echoing_values(environment, setting_name):
    with pytest.raises(ValueError) as exc_info:
        Phase8Settings.from_env(environment)

    message = str(exc_info.value)
    assert setting_name in message


def test_configuration_error_does_not_echo_invalid_secret_like_value():
    unsafe_value = "test-secret-value"

    with pytest.raises(ValueError) as exc_info:
        Phase8Settings.from_env({"PHASE8_OPTIONS_WS_MAX_MESSAGE_BYTES": unsafe_value})

    assert "PHASE8_OPTIONS_WS_MAX_MESSAGE_BYTES" in str(exc_info.value)
    assert unsafe_value not in str(exc_info.value)


def test_enabled_setting_accepts_strict_true_values_and_remains_context_only():
    settings = Phase8Settings.from_env({"PHASE8_OPTIONS_ENABLED": "true"})

    assert settings.enabled is True
    assert not hasattr(settings, "api_key")
    assert not hasattr(settings, "executor_enabled")
