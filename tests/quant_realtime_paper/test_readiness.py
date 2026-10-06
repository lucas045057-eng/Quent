from datetime import datetime, timezone

import pytest

from quant_realtime_paper.gates import (
    ClockMonitor,
    assess_readiness,
    classify_public_data_source,
    retry_delay,
    safety_violations,
)
from quant_realtime_paper.store import SessionStore


def test_real_public_data_requires_bitget_canonical_rows_and_rejects_fixture_mix():
    assert classify_public_data_source([
        {"exchange": "bitget", "source": "bitget_v3_ws"},
        {"exchange": "bitget", "source": "bitget_v3_rest"},
    ]) == "REAL_PUBLIC_DATA"
    assert classify_public_data_source([
        {"exchange": "bitget", "source": "bitget_v3_ws"},
        {"exchange": "local", "source": "synthetic_fixture"},
    ]) == "MIXED_REJECTED"
    assert classify_public_data_source([]) == "UNKNOWN"
    assert classify_public_data_source([
        {"exchange": "bitget", "source": "bitget_unapproved_source"},
    ]) == "UNKNOWN"


def test_readiness_fails_closed_on_any_missing_prerequisite():
    result = assess_readiness({
        "market_data": True,
        "clock": True,
        "database": True,
        "strategy": False,
        "risk": True,
        "paper_engine": False,
        "reconciliation": False,
        "live_disabled": True,
        "execution_wired": False,
    })
    assert result.status == "PAPER NOT READY"
    assert result.final_action == "DO NOT TRADE"
    assert {"strategy", "paper_engine", "reconciliation", "execution_wired"} <= set(result.blockers)


def test_hard_safety_gate_rejects_invalid_values_stale_data_and_duplicate_intent():
    reasons = safety_violations(
        market_fresh=False,
        database_ok=True,
        reconciliation_ok=True,
        price=float("nan"),
        quantity=-1,
        duplicate_intent=True,
        clock_ok=False,
        paper_healthy=True,
        live_disabled=True,
    )
    assert {"STALE_MARKET_DATA", "INVALID_PRICE", "INVALID_QUANTITY", "DUPLICATE_INTENT",
            "CLOCK_ANOMALY"} <= set(reasons)


def test_clock_monitor_detects_wall_clock_jump():
    monitor = ClockMonitor(tolerance_seconds=1)
    base = datetime(2026, 9, 29, tzinfo=timezone.utc)
    assert monitor.observe(base, 10.0) is True
    assert monitor.observe(base.replace(second=10), 11.0) is False


def test_retry_delay_is_exponential_and_capped():
    assert [retry_delay(i, base_seconds=1, maximum_seconds=8) for i in range(1, 6)] == [1, 2, 4, 8, 8]


def test_session_store_persists_resumable_session_and_deduplicates_decisions(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    first = store.start_session({"symbols": ["BTCUSDT", "ETHUSDT"], "data_source": "REAL_PUBLIC_DATA"})
    payload = {"decision": "WAIT", "risk_decision": "REJECTED", "risk_reason": "POLICY_NOT_CONFIGURED"}
    assert store.record_decision(first.session_id, "BTCUSDT", "a" * 64, payload) is True
    assert store.record_decision(first.session_id, "BTCUSDT", "a" * 64, payload) is False
    second = store.start_session({"symbols": ["BTCUSDT", "ETHUSDT"], "data_source": "REAL_PUBLIC_DATA"})
    assert second.resumed_from_session_id == first.session_id
    assert store.session(first.session_id)["state"] == "INTERRUPTED"
    assert store.session(second.session_id)["state"] == "RUNNING"
    assert store.decision_count(first.session_id) == 1

def test_phase2_public_funding_provenance_is_part_of_real_source_classification():
    assert classify_public_data_source([
        {"exchange": "bitget", "source": "bitget_v3_ws"},
        {"exchange": "bitget", "source": "bitget_v3_rest"},
        {"exchange": "bitget", "source": "phase2:bitget"},
    ]) == "REAL_PUBLIC_DATA"
    assert classify_public_data_source([
        {"exchange": "bitget", "source": "bitget_v3_ws"},
        {"exchange": "bitget", "source": "phase2:fixture"},
    ]) == "MIXED_REJECTED"
    assert classify_public_data_source([
        {"exchange": "bitget", "source": "bitget_v3_ws"},
        {"exchange": "local", "source": "phase2:local"},
    ]) == "MIXED_REJECTED"

def test_session_counts_distinguish_submitted_orders_from_actual_fills(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT"], "data_source": "REAL_PUBLIC_DATA"})
    payload = {
        "trade_semantics": "PAPER_EXECUTION_RECORDED",
        "risk_decision": "APPROVED",
        "paper_submitted": True,
        "execution_intent": {"mode": "PAPER", "client_order_id": "Q-test"},
        "execution_results": [{"status": "PARTIALLY_FILLED", "filled_quantity": "0.01"}],
    }
    assert store.record_decision(session.session_id, "BTCUSDT", "c" * 64, payload)

    counts = store.counts(session.session_id)

    assert counts["orders"] == 1
    assert counts["trades"] == 1


from quant_realtime_paper import gates


def test_paper_v1_readiness_has_independent_flags_and_fails_closed():
    assess = getattr(gates, "assess_paper_v1_readiness", None)
    assert callable(assess), "structured Paper V1 readiness API is required"
    checks = {
        "process_ready": True, "data_ready": False, "stage1_ready": True,
        "phase9_ready": True, "policy_ready": True, "risk_ready": True,
        "paper_ready": True, "reconciliation_ready": True, "execution_ready": True,
    }

    result = assess(checks, failure_stage="data", reason_code="STALE_MARKET_DATA")

    assert result.process_ready is True
    assert result.data_ready is False
    assert result.stage1_ready is True
    assert result.phase9_ready is True
    assert result.policy_ready is True
    assert result.risk_ready is True
    assert result.paper_ready is True
    assert result.reconciliation_ready is True
    assert result.execution_ready is False
    assert result.blockers == ("data_ready", "execution_ready")
    assert result.no_trade_classification == "NO_TRADE_BY_STALE_DATA"
    assert result.reason_code == "STALE_MARKET_DATA"


@pytest.mark.parametrize(("stage", "reason", "expected"), [
    ("strategy", "PHASE9_INSUFFICIENT", "NO_TRADE_BY_STRATEGY"),
    ("policy", "POLICY_DISABLED", "NO_TRADE_BY_POLICY_DISABLED"),
    ("risk", "STALE_ACCOUNT", "NO_TRADE_BY_RISK_REJECT"),
    ("data", "STALE_MARKET_DATA", "NO_TRADE_BY_STALE_DATA"),
    ("policy", "INTAKE_TTL_NOT_CONFIGURED", "NO_TRADE_BY_MISSING_TTL"),
    ("risk", "MISSING_EXECUTION_TTL", "NO_TRADE_BY_MISSING_TTL"),
    ("paper", "PAPER_UNAVAILABLE", "NO_TRADE_BY_SYSTEM_NOT_READY"),
])
def test_paper_v1_readiness_keeps_reason_classes_distinct(stage, reason, expected):
    assess = getattr(gates, "assess_paper_v1_readiness", None)
    assert callable(assess), "structured Paper V1 readiness API is required"
    checks = {
        "process_ready": True, "data_ready": True, "stage1_ready": True,
        "phase9_ready": True, "policy_ready": True, "risk_ready": True,
        "paper_ready": True, "reconciliation_ready": True, "execution_ready": True,
    }
    checks[{"data": "data_ready", "strategy": "phase9_ready", "policy": "policy_ready",
            "risk": "risk_ready", "paper": "paper_ready"}[stage]] = False

    result = assess(checks, failure_stage=stage, reason_code=reason)

    assert result.no_trade_classification == expected
    assert result.execution_ready is False
    assert result.reason_code == reason
