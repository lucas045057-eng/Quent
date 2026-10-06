"""Small, strict event-file contract used by offline runtime replay tests."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import gzip
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    event_id: str
    source: str
    symbol: str
    event_type: str
    event_timestamp: datetime
    receive_order: int
    payload: Any
    interval: str | None = None
    sequence: int | None = None
    connection_generation: int | None = None
    fault_event: str | None = None
    exchange_timestamp: datetime | None = None
    fetched_at: datetime | None = None
    processed_at: datetime | None = None


def load_replay(path: Path) -> tuple[ReplayEvent, ...]:
    events: list[ReplayEvent] = []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        lines = tuple(stream)
    for line_number, raw_line in enumerate(lines, 1):
        if not raw_line.strip():
            continue
        row = json.loads(raw_line)
        required = {"event_id", "source", "symbol", "event_type", "event_timestamp", "receive_order", "payload"}
        missing = required - row.keys()
        if missing:
            raise ValueError(f"line {line_number}: missing replay fields {sorted(missing)}")
        timestamp = datetime.fromisoformat(row["event_timestamp"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise ValueError(f"line {line_number}: event_timestamp must be UTC")
        if not isinstance(row["payload"], (dict, list)):
            raise ValueError(f"line {line_number}: payload must be an object or list")
        def optional_utc(field: str) -> datetime | None:
            value = row.get(field)
            if value is None:
                return None
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                raise ValueError(f"line {line_number}: {field} must be UTC")
            return parsed
        events.append(ReplayEvent(
            event_id=row["event_id"], source=row["source"], symbol=row["symbol"],
            event_type=row["event_type"], event_timestamp=timestamp,
            receive_order=row["receive_order"], payload=row["payload"],
            interval=row.get("interval"), sequence=row.get("sequence"),
            connection_generation=row.get("connection_generation"),
            fault_event=row.get("fault_event"),
            exchange_timestamp=optional_utc("exchange_timestamp"),
            fetched_at=optional_utc("fetched_at"), processed_at=optional_utc("processed_at"),
        ))
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("event_id values must be unique")
    orders = [event.receive_order for event in events]
    if orders != sorted(orders) or len(set(orders)) != len(orders):
        raise ValueError("receive_order must be strictly increasing")
    return tuple(events)


def replay_signature(events: tuple[ReplayEvent, ...]) -> dict[str, Any]:
    event_types = Counter(event.event_type for event in events)
    symbols = Counter(event.symbol for event in events if event.symbol != "*")
    faults = [
        (event.event_id, event.receive_order, event.event_timestamp.isoformat(), event.source, event.fault_event)
        for event in events if event.fault_event is not None
    ]
    fault_counts = Counter(event.fault_event for event in events if event.fault_event is not None)
    reset_count = sum(count for fault, count in fault_counts.items() if fault == "WS_RESET")
    canonical = "\n".join(
        json.dumps({
            "event_id": event.event_id, "source": event.source, "symbol": event.symbol,
            "event_type": event.event_type, "event_timestamp": event.event_timestamp.isoformat(),
            "receive_order": event.receive_order, "interval": event.interval,
            "sequence": event.sequence, "connection_generation": event.connection_generation,
            "fault_event": event.fault_event, "payload": event.payload,
            "exchange_timestamp": event.exchange_timestamp.isoformat() if event.exchange_timestamp else None,
            "fetched_at": event.fetched_at.isoformat() if event.fetched_at else None,
            "processed_at": event.processed_at.isoformat() if event.processed_at else None,
        }, sort_keys=True, separators=(",", ":"))
        for event in events
    )
    return {
        "input_event_total": len(events),
        "event_type_counts": dict(sorted(event_types.items())),
        "symbol_counts": dict(sorted(symbols.items())),
        "reconnect_count": reset_count,
        "fault_counts": dict(sorted(fault_counts.items())),
        "fault_sequence": faults,
        "dataset_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
