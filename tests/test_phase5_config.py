import pytest

from quant_phase1.config import Settings


def test_phase5_defaults_are_paper_only_and_bounded():
    settings = Settings.from_env({})

    assert settings.trading_mode == "paper"
    assert settings.phase5_enabled is False
    assert settings.phase5_min_closed_bars == 21
    assert settings.phase5_return_lookback_bars == 3
    assert settings.phase5_max_context_timestamp_skew_seconds == 60
    assert settings.phase5_max_evidence_bytes == 65_536
    assert settings.phase5_ema_fast_period == 9
    assert settings.phase5_ema_slow_period == 21
    assert settings.phase5_volatility_thresholds["1H"] == (0.003, 0.012, 0.025)
    assert settings.phase5_relative_strength_thresholds_pct["4H"] == (-1.0, 1.0)
    assert settings.phase5_context_retention_days == {
        "5m": 30,
        "15m": 90,
        "1H": 180,
        "4H": 365,
    }
    assert settings.phase5_enrichment_retention_days == 30


def test_phase5_rejects_invalid_coverage_and_thresholds():
    with pytest.raises(ValueError, match="PHASE5_MIN_BREADTH_COVERAGE"):
        Settings.from_env({"PHASE5_MIN_BREADTH_COVERAGE": "1.1"})

    with pytest.raises(ValueError, match="PHASE5_MIN_CLOSED_BARS"):
        Settings.from_env({"PHASE5_MIN_CLOSED_BARS": "0"})


def test_phase5_does_not_allow_live_mode_or_private_credentials():
    with pytest.raises(ValueError, match="TRADING_MODE=paper"):
        Settings.from_env({"TRADING_MODE": "live"})
    with pytest.raises(ValueError, match="private API credentials"):
        Settings.from_env({"BITGET_API_SECRET": "must-not-be-used"})


def test_phase5_thresholds_are_configurable_and_ordered():
    settings = Settings.from_env(
        {
            "PHASE5_VOL_LOW_5M": "0.002",
            "PHASE5_VOL_HIGH_5M": "0.005",
            "PHASE5_VOL_EXTREME_5M": "0.010",
            "PHASE5_RS_NEGATIVE_15M_PCT": "0.3",
            "PHASE5_RS_POSITIVE_15M_PCT": "0.4",
        }
    )
    assert settings.phase5_volatility_thresholds["5m"] == (0.002, 0.005, 0.01)
    assert settings.phase5_relative_strength_thresholds_pct["15m"] == (-0.3, 0.4)

    with pytest.raises(ValueError, match="thresholds must increase"):
        Settings.from_env({"PHASE5_VOL_LOW_5M": "0.5", "PHASE5_VOL_HIGH_5M": "0.1"})
