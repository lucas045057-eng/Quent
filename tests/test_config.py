import pytest

from quant_phase1.config import Settings


def test_defaults_are_paper_with_timeframe_specific_retention():
    settings = Settings.from_env({})

    assert settings.trading_mode == "paper"
    assert settings.kline_fetch_limit == 100
    assert settings.market_snapshot_retention_days == 7
    assert settings.kline_retention_days == {"5m": 30, "15m": 90, "1H": 180, "4H": 365}
    assert settings.kline_ingestion_grace_seconds == {"5m": 30, "15m": 60, "1H": 120, "4H": 180}
    assert settings.phase2_enabled is False
    assert settings.phase2_symbols == ()
    assert settings.phase2_interval_seconds == 300
    assert settings.phase2_oi_freshness_seconds == 600
    assert settings.phase2_funding_freshness_seconds == 600
    assert settings.phase2_oi_retention_days == 30
    assert settings.phase2_funding_retention_days == 90
    assert settings.phase2_snapshot_retention_days == 30


def test_private_key_presence_is_rejected_in_phase1():
    with pytest.raises(ValueError, match="private API credentials"):
        Settings.from_env({"BITGET_API_KEY": "secret"})


def test_live_mode_is_rejected_in_phase1():
    with pytest.raises(ValueError, match="TRADING_MODE=paper"):
        Settings.from_env({"TRADING_MODE": "live"})


def test_invalid_retention_is_rejected():
    with pytest.raises(ValueError, match="KLINE_RETENTION_5M_DAYS"):
        Settings.from_env({"KLINE_RETENTION_5M_DAYS": "0"})


def test_phase7_context_scan_limits_are_bounded():
    settings = Settings.from_env({"PHASE7_CONTEXT_LOOKBACK_HOURS": "6", "PHASE7_CONTEXT_WINDOW_BATCH": "32"})

    assert settings.phase7_context_lookback_hours == 6
    assert settings.phase7_context_window_batch == 32
    with pytest.raises(ValueError, match="PHASE7_CONTEXT_LOOKBACK_HOURS"):
        Settings.from_env({"PHASE7_CONTEXT_LOOKBACK_HOURS": "25"})
    with pytest.raises(ValueError, match="PHASE7_CONTEXT_WINDOW_BATCH"):
        Settings.from_env({"PHASE7_CONTEXT_WINDOW_BATCH": "257"})


def test_universe_limit_from_environment_is_bounded_and_effective():
    assert Settings.from_env({}).universe_limit == 200
    assert Settings.from_env({"UNIVERSE_LIMIT": "2"}).universe_limit == 2
    for value in ("0", "-1", "1001", "oops"):
        with pytest.raises(ValueError, match="UNIVERSE_LIMIT"):
            Settings.from_env({"UNIVERSE_LIMIT": value})
