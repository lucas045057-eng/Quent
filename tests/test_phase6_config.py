from __future__ import annotations

import pytest

from quant_phase1.config import Settings


def test_phase6_is_disabled_by_default_and_has_bounded_defaults():
    settings = Settings.from_env({"TRADING_MODE": "paper"})
    assert settings.phase6_enabled is False
    assert settings.trading_mode == "paper"
    assert settings.phase6_news_freshness_hours == 24
    assert settings.phase6_macro_freshness_days == 7
    assert settings.phase6_unlock_freshness_days == 30
    assert settings.phase6_ai_queue_capacity == 32
    assert settings.phase6_ai_max_retries == 1
    assert settings.phase6_ai_retry_interval_seconds == 300
    assert settings.phase6_ai_daily_hard_budget == 20.0


def test_phase6_budget_order_and_bounds_are_validated():
    with pytest.raises(ValueError):
        Settings.from_env({"TRADING_MODE": "paper", "PHASE6_AI_DAILY_SOFT_BUDGET": "3", "PHASE6_AI_DAILY_HARD_BUDGET": "2"})
    with pytest.raises(ValueError):
        Settings.from_env({"TRADING_MODE": "paper", "PHASE6_AI_QUEUE_CAPACITY": "0"})


def test_phase6_provider_policy_is_config_only_and_keys_are_not_required():
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE6_ENABLED": "1",
        "PHASE6_AI_PRIMARY_PROVIDER": "local-fake",
        "PHASE6_AI_FALLBACK_PROVIDER": "",
        "PHASE6_AI_DAILY_SOFT_BUDGET": "1.5",
    })
    assert settings.phase6_enabled is True
    assert settings.phase6_ai_primary_provider == "local-fake"
    assert settings.phase6_ai_fallback_provider == ""
    assert settings.phase6_ai_daily_soft_budget == 1.5
