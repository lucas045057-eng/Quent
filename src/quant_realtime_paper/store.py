"""Durable operations ledger; canonical market data remains in PostgreSQL."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from uuid import uuid4


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


@dataclass(frozen=True, slots=True)
class SessionRef:
    session_id: str
    resumed_from_session_id: str | None


class SessionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS paper_sessions (
                    session_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    state TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    git_commit TEXT,
                    data_source TEXT NOT NULL,
                    resumed_from_session_id TEXT,
                    stop_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS decision_records (
                    decision_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES paper_sessions(session_id),
                    symbol TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS decision_records_session_time
                    ON decision_records(session_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS cycle_snapshots (
                    session_id TEXT PRIMARY KEY REFERENCES paper_sessions(session_id),
                    captured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_cycles (
                    cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES paper_sessions(session_id),
                    observed_at TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    readiness TEXT NOT NULL,
                    final_action TEXT NOT NULL,
                    readiness_detail_json TEXT
                );
                CREATE INDEX IF NOT EXISTS runtime_cycles_session_time
                    ON runtime_cycles(session_id, observed_at);
                CREATE TABLE IF NOT EXISTS runtime_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES paper_sessions(session_id),
                    event_time TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(paper_sessions)")}
            for name, definition in (("expected_duration_seconds", "REAL"), ("poll_seconds", "REAL")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE paper_sessions ADD COLUMN {name} {definition}")
            cycle_columns = {row["name"] for row in connection.execute("PRAGMA table_info(runtime_cycles)")}
            if "readiness_detail_json" not in cycle_columns:
                connection.execute("ALTER TABLE runtime_cycles ADD COLUMN readiness_detail_json TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start_session(self, metadata: Mapping[str, Any], *, now: datetime | None = None) -> SessionRef:
        at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        session_id = uuid4().hex[:24]
        with self._connection() as connection:
            previous = connection.execute(
                "SELECT session_id FROM paper_sessions WHERE state='RUNNING' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            resumed = previous["session_id"] if previous else None
            if resumed:
                connection.execute(
                    "UPDATE paper_sessions SET state='INTERRUPTED', ended_at=?, stop_reason='PROCESS_RESTART' WHERE session_id=?",
                    (at, resumed),
                )
            connection.execute(
                """INSERT INTO paper_sessions(session_id,started_at,state,symbols_json,strategy_version,
                   config_hash,git_commit,data_source,expected_duration_seconds,poll_seconds,resumed_from_session_id)
                   VALUES(?,?, 'RUNNING',?,?,?,?,?,?,?,?)""",
                (session_id, at, _json(metadata.get("symbols", [])), str(metadata.get("strategy_version", "unknown")),
                 str(metadata.get("config_hash", "unknown")), metadata.get("git_commit"),
                 str(metadata.get("data_source", "UNKNOWN")), metadata.get("expected_duration_seconds"),
                 metadata.get("poll_seconds"), resumed),
            )
        return SessionRef(session_id, resumed)

    def set_data_source(self, session_id: str, source: str) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE paper_sessions SET data_source=? WHERE session_id=?", (source, session_id))

    def record_decision(
        self, session_id: str, symbol: str, input_digest: str, payload: Mapping[str, Any],
        *, data_source: str = "UNKNOWN", now: datetime | None = None,
    ) -> bool:
        if len(input_digest) != 64 or any(ch not in "0123456789abcdef" for ch in input_digest.lower()):
            raise ValueError("input_digest must be a SHA-256 hex digest")
        key = f"{symbol}:{input_digest.lower()}"
        created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO decision_records
                   (decision_key,session_id,symbol,input_digest,created_at,data_source,payload_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (key, session_id, symbol, input_digest.lower(), created, data_source, _json(payload)),
            )
            return cursor.rowcount == 1

    def record_cycle(
        self, session_id: str, *, data_source: str, readiness: str, final_action: str,
        readiness_detail: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        allowed_sources = {"REAL_PUBLIC_DATA", "SYNTHETIC_FIXTURE", "MIXED_REJECTED", "UNKNOWN"}
        if data_source not in allowed_sources:
            raise ValueError("unsupported data source classification")
        observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO runtime_cycles(
                       session_id,observed_at,data_source,readiness,final_action,readiness_detail_json
                   ) VALUES(?,?,?,?,?,?)""",
                (session_id, observed, data_source, readiness, final_action,
                 _json(readiness_detail) if readiness_detail is not None else None),
            )

    def cycle_history(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT observed_at,data_source,readiness,final_action,readiness_detail_json
                   FROM runtime_cycles WHERE session_id=? ORDER BY observed_at,cycle_id""",
                (session_id,),
            ).fetchall()
        history = []
        for row in rows:
            cycle = dict(row)
            raw_detail = cycle.pop("readiness_detail_json")
            cycle["readiness_detail"] = json.loads(raw_detail) if raw_detail else None
            history.append(cycle)
        return history

    def decision_sources(self, session_id: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT data_source FROM decision_records WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [str(row["data_source"]) for row in rows]
    def record_snapshot(self, session_id: str, payload: Mapping[str, Any], *, now: datetime | None = None) -> None:
        captured = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO cycle_snapshots(session_id,captured_at,payload_json) VALUES(?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET captured_at=excluded.captured_at,payload_json=excluded.payload_json""",
                (session_id, captured, _json(payload)),
            )

    def record_event(self, session_id: str, kind: str, reason: str, details: Mapping[str, Any] | None = None,
                     *, now: datetime | None = None) -> None:
        at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO runtime_events(session_id,event_time,kind,reason,details_json) VALUES(?,?,?,?,?)",
                (session_id, at, kind, reason, _json(details or {})),
            )

    def end_session(self, session_id: str, *, reason: str = "DURATION_COMPLETE", now: datetime | None = None) -> None:
        at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                "UPDATE paper_sessions SET state='STOPPED',ended_at=?,stop_reason=? WHERE session_id=? AND state='RUNNING'",
                (at, reason, session_id),
            )

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM paper_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["symbols"] = json.loads(result.pop("symbols_json"))
        return result

    def decision_count(self, session_id: str) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT count(*) FROM decision_records WHERE session_id=?", (session_id,)).fetchone()[0])

    def counts(self, session_id: str) -> dict[str, int]:
        with self._connection() as connection:
            payloads = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM decision_records WHERE session_id=?", (session_id,),
            ).fetchall()]
            errors = int(connection.execute(
                "SELECT count(*) FROM runtime_events WHERE session_id=? AND kind='ERROR'", (session_id,),
            ).fetchone()[0])
        decisions = len(payloads)
        rejects = sum(
            str(payload.get("risk_decision", "")).startswith("REJECT")
            for payload in payloads
        )
        orders = sum(payload.get("paper_submitted") is True for payload in payloads)
        trades = 0
        for payload in payloads:
            fills = []
            for result in payload.get("execution_results", ()):
                try:
                    quantity = Decimal(str(result.get("filled_quantity", "0")))
                except (InvalidOperation, AttributeError, TypeError):
                    continue
                if quantity.is_finite() and quantity > 0:
                    fills.append(quantity)
            trades += bool(fills)
        return {"orders": int(orders), "trades": int(trades), "decisions": decisions,
                "risk_rejects": int(rejects), "errors": errors}

    def latest_snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        with self._connection() as connection:
            if session_id:
                row = connection.execute("SELECT captured_at,payload_json FROM cycle_snapshots WHERE session_id=?", (session_id,)).fetchone()
            else:
                row = connection.execute("""SELECT c.captured_at,c.payload_json FROM cycle_snapshots c
                    JOIN paper_sessions s USING(session_id) ORDER BY c.captured_at DESC LIMIT 1""").fetchone()
        return {"captured_at": row["captured_at"], "payload": json.loads(row["payload_json"])} if row else {}

    @staticmethod
    def readonly_snapshot(path: str | Path, *, decision_limit: int = 20) -> dict[str, Any]:
        target = Path(path)
        if not target.is_file():
            return {"session": None, "snapshot": None, "decisions": [], "events": [], "cycles": []}
        uri = target.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        try:
            current = connection.execute("SELECT * FROM paper_sessions WHERE state='RUNNING' ORDER BY started_at DESC LIMIT 1").fetchone()
            if current is None:
                current = connection.execute("SELECT * FROM paper_sessions ORDER BY started_at DESC LIMIT 1").fetchone()
            if current is None:
                return {"session": None, "snapshot": None, "decisions": [], "events": [], "cycles": []}
            session = dict(current)
            session["symbols"] = json.loads(session.pop("symbols_json"))
            snap = connection.execute("SELECT captured_at,payload_json FROM cycle_snapshots WHERE session_id=?", (session["session_id"],)).fetchone()
            decision_rows = connection.execute("SELECT symbol,created_at,data_source,payload_json FROM decision_records WHERE session_id=? ORDER BY created_at DESC LIMIT ?", (session["session_id"], min(decision_limit, 100))).fetchall()
            event_rows = connection.execute("SELECT event_time,kind,reason,details_json FROM runtime_events WHERE session_id=? ORDER BY event_id DESC LIMIT 50", (session["session_id"],)).fetchall()
            cycle_columns = {row["name"] for row in connection.execute("PRAGMA table_info(runtime_cycles)")}
            detail_column = ",readiness_detail_json" if "readiness_detail_json" in cycle_columns else ""
            cycle_rows = connection.execute(
                f"SELECT observed_at,data_source,readiness,final_action{detail_column} "
                "FROM runtime_cycles WHERE session_id=? ORDER BY observed_at DESC,cycle_id DESC LIMIT 100",
                (session["session_id"],),
            ).fetchall()
            recent_cycles = []
            for row in reversed(cycle_rows):
                cycle = {key: row[key] for key in ("observed_at", "data_source", "readiness", "final_action")}
                raw_detail = row["readiness_detail_json"] if "readiness_detail_json" in cycle_columns else None
                cycle["readiness_detail"] = json.loads(raw_detail) if raw_detail else None
                recent_cycles.append(cycle)
            return {
                "session": session,
                "snapshot": {"captured_at": snap["captured_at"], **json.loads(snap["payload_json"])} if snap else None,
                "decisions": [{"symbol": row["symbol"], "created_at": row["created_at"], "data_source": row["data_source"], **json.loads(row["payload_json"])} for row in decision_rows],
                "events": [{"event_time": row["event_time"], "kind": row["kind"], "reason": row["reason"], "details": json.loads(row["details_json"])} for row in event_rows],
                "cycles": recent_cycles,
            }
        finally:
            connection.close()
