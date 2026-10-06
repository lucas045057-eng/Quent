import pytest

from quant_phase1.config import Settings


def test_phase3_defaults_are_bounded_and_retention_is_timeframe_specific():
    settings = Settings.from_env({})

    assert settings.trading_mode == "paper"
    assert settings.phase3_enabled is False
    assert settings.max_trade_stream_symbols == 20
    assert settings.max_trade_queue_size == 2000
    assert settings.phase3_flow_retention_days == {
        "1m": 7,
        "5m": 30,
        "15m": 90,
        "1H": 180,
        "4H": 365,
    }
    assert settings.phase3_cvd_retention_days == 365
    assert settings.phase3_gap_retention_days == 90
    assert settings.phase3_cross_exchange_retention_days == 180
    assert settings.phase3_enrichment_retention_days == 90
    assert settings.phase3_min_directional_sources == 2


def test_phase3_settings_parse_overrides_and_enable_only_explicitly():
    settings = Settings.from_env(
        {
            "TRADING_MODE": "paper",
            "PHASE3_ENABLED": "1",
            "MAX_TRADE_STREAM_SYMBOLS": "12",
            "TRADE_MIN_SUBSCRIPTION_SECONDS": "120",
            "TRADE_SUBSCRIPTION_COOLDOWN_SECONDS": "30",
            "MAX_TRADE_QUEUE_SIZE": "64",
            "TRADE_DEDUP_MAX_ENTRIES_PER_EXCHANGE": "100",
            "TRADE_DEDUP_TTL_SECONDS": "90",
            "TRADE_ALLOWED_LATENESS_SECONDS": "7",
            "PHASE3_FLOW_RETENTION_1M_DAYS": "2",
            "PHASE3_FLOW_RETENTION_4H_DAYS": "30",
            "PHASE3_CVD_RETENTION_DAYS": "60",
            "PHASE3_GAP_RETENTION_DAYS": "14",
            "PHASE3_CROSS_EXCHANGE_RETENTION_DAYS": "30",
            "PHASE3_ENRICHMENT_RETENTION_DAYS": "15",
            "PHASE3_MIN_DIRECTIONAL_SOURCES": "1",
        }
    )

    assert settings.phase3_enabled is True
    assert settings.max_trade_stream_symbols == 12
    assert settings.trade_min_subscription_seconds == 120
    assert settings.trade_subscription_cooldown_seconds == 30
    assert settings.max_trade_queue_size == 64
    assert settings.trade_dedup_max_entries_per_exchange == 100
    assert settings.trade_dedup_ttl_seconds == 90
    assert settings.trade_allowed_lateness_seconds == 7
    assert settings.phase3_flow_retention_days["1m"] == 2
    assert settings.phase3_flow_retention_days["4H"] == 30
    assert settings.phase3_cvd_retention_days == 60
    assert settings.phase3_gap_retention_days == 14
    assert settings.phase3_cross_exchange_retention_days == 30
    assert settings.phase3_enrichment_retention_days == 15
    assert settings.phase3_min_directional_sources == 1


@pytest.mark.parametrize(
    "name",
    [
        "MAX_TRADE_STREAM_SYMBOLS",
        "MAX_TRADE_QUEUE_SIZE",
        "TRADE_DEDUP_MAX_ENTRIES_PER_EXCHANGE",
        "TRADE_DEDUP_TTL_SECONDS",
        "PHASE3_FLOW_RETENTION_1M_DAYS",
        "PHASE3_CVD_RETENTION_DAYS",
        "PHASE3_GAP_RETENTION_DAYS",
        "PHASE3_CROSS_EXCHANGE_RETENTION_DAYS",
        "PHASE3_ENRICHMENT_RETENTION_DAYS",
    ],
)
def test_phase3_positive_limits_reject_zero(name):
    with pytest.raises(ValueError, match=name):
        Settings.from_env({name: "0"})


def test_phase3_keeps_phase1_private_credential_and_live_guards():
    with pytest.raises(ValueError, match="private API credentials"):
        Settings.from_env({"BITGET_API_SECRET": "must-not-be-read"})
    with pytest.raises(ValueError, match="TRADING_MODE=paper"):
        Settings.from_env({"TRADING_MODE": "live"})
