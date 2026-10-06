from datetime import datetime, timedelta, timezone

from quant_realtime_paper.acceptance import evaluate_formal_acceptance


def _fixture(source="REAL_PUBLIC_DATA", readiness="PAPER READY", final_action="PAPER LOOP READY"):
    started = datetime(2026, 9, 28, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=40)
    session = {
        "session_id": "paper-1",
        "state": "STOPPED",
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "poll_seconds": 10,
    }
    cycles = [
        {"observed_at": (started + timedelta(seconds=offset)).isoformat(),
         "data_source": source, "readiness": readiness, "final_action": final_action}
        for offset in (1, 11, 21, 31, 39)
    ]
    decisions = [source]
    return session, cycles, decisions


def test_formal_acceptance_passes_only_full_real_ready_coverage():
    session, cycles, decisions = _fixture()
    report = evaluate_formal_acceptance(
        session, cycles, decisions, minimum_duration_seconds=40,
    )
    assert report["passed"] is True
    assert report["status"] == "PASS"
    assert report["source_counts"] == {"REAL_PUBLIC_DATA": 5}


def test_formal_acceptance_rejects_fixture_unknown_and_mixed_sources():
    for source in ("SYNTHETIC_FIXTURE", "UNKNOWN", "MIXED_REJECTED"):
        session, cycles, decisions = _fixture(source=source)
        report = evaluate_formal_acceptance(
            session, cycles, decisions, minimum_duration_seconds=40,
        )
        assert report["passed"] is False
        assert "NON_REAL_DATA_SOURCE" in report["blockers"]


def test_formal_acceptance_rejects_incomplete_or_unready_sessions():
    session, cycles, decisions = _fixture(readiness="PAPER NOT READY", final_action="DO NOT TRADE")
    short = evaluate_formal_acceptance(
        session, cycles, decisions, minimum_duration_seconds=86400,
    )
    assert short["passed"] is False
    assert "DURATION_INCOMPLETE" in short["blockers"]
    assert "PAPER_NOT_READY" in short["blockers"]
def test_cycle_ledger_keeps_source_history_across_sessions(tmp_path):
    from quant_realtime_paper.store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT"], "data_source": "UNKNOWN",
                                   "poll_seconds": 10, "expected_duration_seconds": 86400})
    at = datetime(2026, 9, 29, tzinfo=timezone.utc)
    store.record_cycle(session.session_id, data_source="UNKNOWN", readiness="PAPER NOT READY",
                       final_action="DO NOT TRADE", now=at)
    store.record_cycle(session.session_id, data_source="REAL_PUBLIC_DATA", readiness="PAPER NOT READY",
                       final_action="DO NOT TRADE", now=at + timedelta(seconds=10))
    history = store.cycle_history(session.session_id)
    assert [row["data_source"] for row in history] == ["UNKNOWN", "REAL_PUBLIC_DATA"]
    assert store.session(session.session_id)["expected_duration_seconds"] == 86400
    assert store.session(session.session_id)["poll_seconds"] == 10


def test_cycle_readiness_detail_is_persisted_and_acceptance_reports_reason_counts(tmp_path):
    from quant_realtime_paper.store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3")
    session_ref = store.start_session({"symbols": ["BTCUSDT"], "data_source": "UNKNOWN"})
    detail = {
        "process_ready": True, "data_ready": True, "stage1_ready": True,
        "phase9_ready": False, "policy_ready": False, "risk_ready": True,
        "paper_ready": False, "reconciliation_ready": False, "execution_ready": False,
        "blockers": ("phase9_ready", "policy_ready", "paper_ready",
                     "reconciliation_ready", "execution_ready"),
        "no_trade_classification": "NO_TRADE_BY_POLICY_DISABLED",
        "reason_code": "POLICY_DISABLED",
        "status": "PAPER NOT READY", "final_action": "DO NOT TRADE",
    }
    store.record_cycle(
        session_ref.session_id, data_source="REAL_PUBLIC_DATA",
        readiness="PAPER NOT READY", final_action="DO NOT TRADE",
        readiness_detail=detail,
    )
    cycles = store.cycle_history(session_ref.session_id)

    assert cycles[0]["readiness_detail"] == {**detail, "blockers": list(detail["blockers"])}
    status = store.readonly_snapshot(store.path)
    assert status["cycles"][0]["readiness_detail"] == cycles[0]["readiness_detail"]
    report_session, formal_cycles, decisions = _fixture()
    report = evaluate_formal_acceptance(
        report_session, [*formal_cycles, cycles[0]], decisions,
        minimum_duration_seconds=40,
    )
    assert report["no_trade_classification_counts"] == {
        "NO_TRADE_BY_POLICY_DISABLED": 1,
    }
    assert report["structured_readiness_cycle_count"] == 1
    assert report["passed"] is False
