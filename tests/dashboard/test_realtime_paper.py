from datetime import datetime, timezone

from dashboard.backend.app import create_app
from dashboard.backend.config import DashboardConfig
from quant_realtime_paper.store import SessionStore


def client(tmp_path):
    from fastapi.testclient import TestClient
    return TestClient(create_app(DashboardConfig(root=tmp_path)), base_url="http://127.0.0.1:3000")


def test_realtime_paper_endpoint_is_honest_when_not_started(tmp_path):
    response = client(tmp_path).get("/api/realtime-paper")
    assert response.status_code == 200
    body = response.json()
    assert body["availability"] == "NO_DATA"
    assert body["data"]["runtime"] is None
    assert "REALTIME_PAPER_NOT_STARTED" in body["warnings"]


def test_realtime_paper_endpoint_shows_live_source_and_blocked_gate(tmp_path):
    store = SessionStore(tmp_path / "var/realtime-paper/sessions.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT", "ETHUSDT"], "data_source": "REAL_PUBLIC_DATA",
                                   "config_hash": "a" * 64, "strategy_version": "phase1-basic-v1"})
    store.record_snapshot(session.session_id, {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "mode": "REALTIME PAPER", "data_source": "REAL_PUBLIC_DATA",
        "readiness": "PAPER NOT READY", "final_action": "DO NOT TRADE",
        "counts": {"orders": 0, "trades": 0, "decisions": 1, "risk_rejects": 1, "errors": 0},
        "symbols": [{"symbol": "BTCUSDT"}],
        "health": {
            "process_alive": {"status": "ALIVE"},
            "collector_heartbeat": {"status": "AVAILABLE", "checked_at": datetime.now(timezone.utc).isoformat()},
            "fresh_market_data": {"status": "STALE"},
        },
    })
    body = client(tmp_path).get("/api/realtime-paper").json()
    assert body["availability"] == "AVAILABLE"
    assert body["data"]["data_source"] == "REAL_PUBLIC_DATA"
    assert body["data"]["runtime"]["final_action"] == "DO NOT TRADE"
    assert body["data"]["runtime"]["health"]["process_alive"]["status"] == "ALIVE"
    assert body["data"]["runtime"]["health"]["collector_heartbeat"]["status"] == "AVAILABLE"
    assert body["data"]["runtime"]["health"]["fresh_market_data"]["status"] == "STALE"
    assert "PAPER_NOT_READY" in body["warnings"]
