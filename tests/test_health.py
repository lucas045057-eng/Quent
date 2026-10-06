import logging
from datetime import datetime, timezone

from quant_phase1.contracts import DataStatus
from quant_phase1.health import ComponentHealth, HealthRegistry
from quant_phase1.runtime import RuntimeHealthTracker
from quant_phase1.service import mark_degraded


def test_health_registry_aggregates_component_status_without_order_components():
    registry = HealthRegistry()
    now = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)
    registry.set(ComponentHealth("bitget_rest", DataStatus.AVAILABLE, now, {"endpoint": "v3"}))
    registry.set(ComponentHealth("bitget_ws", DataStatus.STALE, now, {"reason": "reconnecting"}))
    assert registry.overall().status is DataStatus.STALE
    assert set(registry.snapshot()) == {"bitget_rest", "bitget_ws"}


def test_error_health_blocks_new_stage1_candidates():
    registry = HealthRegistry()
    now = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)
    registry.set(ComponentHealth("postgres", DataStatus.ERROR, now, {"reason": "unavailable"}))
    assert registry.overall().status is DataStatus.ERROR
    assert registry.allows_stage1() is False


def test_runtime_health_log_and_health_file_use_only_safe_error_summary(monkeypatch, caplog):
    tracker = RuntimeHealthTracker("quant-collector")
    written = []
    monkeypatch.setattr(
        "quant_phase1.service.write_health_file",
        lambda component, state, *, reason=None: written.append((component, state, reason)),
    )
    secret = "api-key: test-secret-value https://rpc.example/private/token raw_payload=large"

    with caplog.at_level(logging.WARNING, logger="quant_phase1"):
        mark_degraded(tracker, "quant-collector", RuntimeError(secret))

    assert "test-secret-value" not in caplog.text
    assert "rpc.example" not in caplog.text
    assert "raw_payload" not in caplog.text
    assert "test-secret-value" not in repr(written)
    assert tracker.snapshot().last_error.category.value == "UNKNOWN"
