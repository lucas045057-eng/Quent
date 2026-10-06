from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase1.contracts import DataStatus, RawReference
from quant_phase1.stage1 import Stage1Result
from quant_phase9.canonical import canonical_bytes, canonical_json
from quant_phase9.intake import (
    Phase9OutboxWriter,
    build_stage1_candidate_event,
)


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _result(*, category: str = "A", status: DataStatus = DataStatus.AVAILABLE, details: str = "") -> Stage1Result:
    return Stage1Result(
        symbol="BTCUSDT",
        category=category,
        reason="fixture result",
        status=status,
        inputs_used=("price", "closed_5m"),
        indicators={"atr": Decimal("1.2500")},
        structure="BULLISH",
        reason_codes=("STRUCTURE_ALIGNED",),
        key_metrics={"detail": details},
        data_snapshot_reference=RawReference(
            provider="phase1", endpoint="canonical_market_snapshot", request_id="do-not-copy",
            reason="stage1_inputs",
        ),
        timestamp=NOW,
    )


def _event(result: Stage1Result):
    return build_stage1_candidate_event(
        screening_result_id=17,
        run_id=8,
        screening_result=result,
        market="USDT_PERPETUAL",
        instrument_scope={
            "category": "USDT-FUTURES",
            "quote_coin": "USDT",
            "contract_type": "perpetual",
            "status": "online",
        },
        candidate_created_at=NOW,
        candidate_valid_until=None,
        stage1_policy_version="phase1-basic-v1",
        source_as_of=NOW,
    )


@pytest.mark.parametrize(
    ("category", "status"),
    [("A", DataStatus.AVAILABLE), ("B", DataStatus.STALE), ("A", DataStatus.NOT_AVAILABLE)],
)
def test_candidate_event_identity_is_stable_and_preserves_stage1_status(category, status):
    event = _event(_result(category=category, status=status))
    repeated = _event(_result(category=category, status=status))

    assert event.event_id == repeated.event_id
    assert len(event.event_id) == 64
    assert event.event_id == event.event_id.lower()
    assert event.canonical_payload_digest == repeated.canonical_payload_digest
    assert event.canonical_payload["stage1_projection"]["category"] == category
    assert event.canonical_payload["stage1_projection"]["status"] == status.value
    assert event.canonical_payload["stage1_projection"]["indicators"]["atr"] == Decimal("1.2500")
    assert any(ref.startswith("phase1:snapshot:") for ref in event.source_refs)
    assert "do-not-copy" not in canonical_json(event.canonical_payload)


def test_event_payload_size_is_checked_without_truncation_or_database_write():
    event = _event(_result(details="x" * (64 * 1024)))

    class RecordingConnection:
        def __init__(self):
            self.calls = []

        def execute(self, query, params):
            self.calls.append((query, params))

    connection = RecordingConnection()
    with pytest.raises(ValueError, match="64 KiB"):
        Phase9OutboxWriter().emit(connection, event=event)
    assert connection.calls == []
    assert "x" * 128 in canonical_bytes(event.canonical_payload).decode("utf-8")


def test_outbox_writer_uses_only_caller_connection_and_does_not_commit():
    event = _event(_result())

    class RecordingConnection:
        def __init__(self):
            self.calls = []

        def execute(self, query, params):
            self.calls.append((str(query), params))
            return self

        def fetchone(self):
            return (1,)

    connection = RecordingConnection()
    Phase9OutboxWriter().emit(connection, event=event)

    assert len(connection.calls) == 1
    query, params = connection.calls[0]
    assert "INSERT INTO outbox_events" in query
    assert "phase9_state" in query
    assert params[0] == "phase9.stage1_candidate"
