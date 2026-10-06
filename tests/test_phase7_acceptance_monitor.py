from datetime import datetime, timezone
import json

from scripts import phase7_acceptance_monitor as monitor


def test_collector_diagnostics_reader_keeps_only_approved_scalar_metrics(monkeypatch):
    sampled_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        monitor,
        "_run",
        lambda _args: json.dumps({
            "sampled_at_utc": sampled_at,
            "phase7_bitcoin_active_rpc": 1,
            "phase7_bitcoin_stage": "BLOCK_RPC",
            "PHASE7_BITCOIN_RPC_URL": "https://provider.invalid/path-secret",
            "payload": {"transaction": "must-not-escape"},
        }),
    )

    result = monitor._collector_diagnostics_sample("phase7")

    assert result["phase7_bitcoin_active_rpc"] == 1
    assert result["phase7_bitcoin_stage"] == "BLOCK_RPC"
    assert result["snapshot_age_seconds"] >= 0
    assert "path-secret" not in repr(result)
    assert "transaction" not in repr(result)
