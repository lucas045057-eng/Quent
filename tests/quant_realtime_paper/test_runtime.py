from datetime import datetime, timezone
from pathlib import Path

import pytest

from quant_realtime_paper.config import RuntimeConfig
from quant_realtime_paper.runtime import RealtimePaperMonitor
from quant_realtime_paper.store import SessionStore


def test_monitor_database_failure_is_persisted_and_never_creates_order(tmp_path):
    store = SessionStore(tmp_path / "runtime.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT", "ETHUSDT"], "data_source": "UNKNOWN"})
    config = RuntimeConfig(None, tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT", "ETHUSDT"), tmp_path)
    monitor = RealtimePaperMonitor(config, store=store, environ={"TRADING_MODE": "paper", "LIVE_ENABLED": "true"})
    monitor._session_id = session.session_id
    result = monitor.cycle(now=datetime.now(timezone.utc))
    assert result["readiness"] == "PAPER NOT READY"
    assert result["final_action"] == "DO NOT TRADE"
    detail = result.get("readiness_detail")
    assert detail is not None, "monitor must expose independent Paper V1 readiness flags"
    assert detail["process_ready"] is True
    assert detail["data_ready"] is False
    assert detail["execution_ready"] is False
    assert detail["no_trade_classification"] == "NO_TRADE_BY_SYSTEM_NOT_READY"
    assert detail["reason_code"] == "CANONICAL_DATABASE_UNAVAILABLE"
    assert "database" in result["blockers"]
    assert result["data_source"] == "UNKNOWN"
    assert result["counts"]["orders"] == 0
    assert result["counts"]["trades"] == 0
    persisted = store.latest_snapshot(session.session_id)["payload"]
    assert persisted["final_action"] == "DO NOT TRADE"
    assert store.counts(session.session_id)["errors"] == 1
    cycle=store.cycle_history(session.session_id)[0]
    assert cycle['readiness_detail']['reason_code']=='CANONICAL_DATABASE_UNAVAILABLE'
    assert cycle['readiness_detail']['data_ready'] is False
def test_canonical_reader_preserves_existing_tuple_row_contract(tmp_path, monkeypatch):
    import psycopg
    from quant_realtime_paper import runtime

    class Cursor:
        def __init__(self, row=None, rows=()):
            self.row = row
            self.rows = rows
        def fetchone(self):
            return self.row
        def fetchall(self):
            return list(self.rows)

    executed = []

    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def execute(self, statement, *args):
            executed.append((statement, args[0] if args else None))
            if "SELECT 1 AS ok" in statement:
                return Cursor((1,))
            if "FROM system_health" in statement:
                return Cursor(None)
            if "FROM funding_rates" in statement:
                return Cursor(rows=())
            if any(f"FROM {table}" in statement for table in ("open_interest", "trade_flow_windows", "liquidation_events")):
                return Cursor(rows=())
            raise AssertionError(f"unexpected query: {statement}")

    def connect(*_args, **kwargs):
        assert "row_factory" not in kwargs
        return Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(runtime, "assert_schema_ready", lambda _connection: None)
    monkeypatch.setattr(runtime.Phase1Repository, "load_latest_market_batch",
                        lambda *_args, **_kwargs: None)
    config = RuntimeConfig("dsn", tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT", "ETHUSDT"), tmp_path)
    monitor = RealtimePaperMonitor(config, store=SessionStore(config.state_db), environ={})
    now = datetime.now(timezone.utc)
    batch, health, funding, db = monitor._read_canonical(now)
    trade_query, trade_params = next(
        (statement, params) for statement, params in executed
        if "FROM trade_flow_windows" in statement
    )
    assert "window_close <= %s" in trade_query
    assert "status='AVAILABLE'" in trade_query
    assert trade_params[1] == now
    assert batch is None
    assert health == {}
    assert funding == []
    assert db["database"] is True
def test_market_passes_existing_stage1_grace_thresholds_to_freshness(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import timedelta
    from decimal import Decimal
    from quant_phase1.contracts import Candle, DataStatus, Ticker
    from quant_phase1.pipeline import MarketDataBatch
    from quant_realtime_paper import runtime

    now = datetime.now(timezone.utc)
    ticker = Ticker("BTCUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1"),
                    Decimal("1"), Decimal("1"), Decimal("1000"), Decimal("100000"),
                    Decimal("100"), Decimal("100"), now, now, now, DataStatus.AVAILABLE, {},
                    source="bitget_v3_ws", exchange="bitget")
    candles = {}
    for interval, seconds in (("5m", 300), ("15m", 900), ("1H", 3600), ("4H", 14400)):
        opened = now - timedelta(seconds=seconds)
        candles[interval] = [Candle("BTCUSDT", interval, opened, Decimal("99"), Decimal("101"),
                                    Decimal("98"), Decimal("100"), Decimal("10"), Decimal("1000"),
                                    opened, now, now, DataStatus.AVAILABLE, True, [],
                                    source="bitget_v3_ws", exchange="bitget")]
    batch = MarketDataBatch(now, [], [ticker], ("BTCUSDT",), {"BTCUSDT": candles})
    expected = {"5m": 30, "15m": 60, "1H": 120, "4H": 180}
    observed = {}

    def freshness(_now, interval, _opened, *, grace_seconds):
        observed[interval] = grace_seconds
        return DataStatus.AVAILABLE

    result = SimpleNamespace(
        symbol="BTCUSDT", category="B", classification="WAIT_TRIGGER", reason="WAIT_FOR_TRIGGER",
        status=DataStatus.AVAILABLE, reason_codes=(), structure="BULLISH", indicators={}, key_metrics={},
    )
    monkeypatch.setattr(runtime, "evaluate_kline_freshness", freshness)
    monkeypatch.setattr(runtime, "stage1_results", lambda *_args, **_kwargs: [result])
    config = RuntimeConfig("dsn", tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RealtimePaperMonitor(config, store=SessionStore(config.state_db), environ={})
    funding_row = {
        "symbol": "BTCUSDT", "canonical_symbol": "BTCUSDT", "exchange": "bitget",
        "exchange_timestamp": now.isoformat(), "fetched_at": now.isoformat(),
        "processed_at": now.isoformat(), "status": "AVAILABLE",
        "normalized_8h_rate": Decimal("0.0001"), "next_funding_time": None,
    }
    rows, results, source, market_ready, _ = monitor._market(batch, [funding_row], now)
    assert source == "REAL_PUBLIC_DATA"
    assert market_ready is True
    assert results == [result]
    assert observed == expected
    assert next(feed for feed in rows[0]["feeds"] if feed["kind"] == "FUNDING")["status"] == "HEALTHY"
    rows_without_funding, _, no_funding_source, ready_without_funding, _ = monitor._market(batch, [], now)
    assert no_funding_source == "MIXED_REJECTED"
    assert ready_without_funding is False
    assert next(feed for feed in rows_without_funding[0]["feeds"] if feed["kind"] == "FUNDING")["status"] == "STALE"
    _, _, unapproved_source, _, _ = monitor._market(batch, [{**funding_row, "exchange": "local"}], now)
    assert unapproved_source == "MIXED_REJECTED"

def test_phase2_funding_accepts_persisted_iso_timestamps(tmp_path):
    from datetime import timedelta
    from decimal import Decimal

    now = datetime.now(timezone.utc)
    event_time = now - timedelta(seconds=30)
    row = {
        "symbol": "BTCUSDT", "canonical_symbol": "BTCUSDT", "exchange": "bitget",
        "exchange_timestamp": event_time.isoformat(), "fetched_at": now.isoformat(),
        "processed_at": now.isoformat(), "status": "AVAILABLE",
        "normalized_8h_rate": Decimal("0.0001"), "next_funding_time": None,
    }
    config = RuntimeConfig("dsn", tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RealtimePaperMonitor(config, store=SessionStore(config.state_db), environ={})
    funding = monitor._phase2_funding("BTCUSDT", [row], now)
    assert funding["status"] == "HEALTHY"
    assert funding["age_seconds"] == 30.0
def test_invalid_numeric_market_data_blocks_stage1_and_is_reported_invalid(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import timedelta
    from decimal import Decimal
    from quant_phase1.contracts import Candle, DataStatus, Ticker
    from quant_phase1.pipeline import MarketDataBatch
    from quant_realtime_paper import runtime

    now = datetime.now(timezone.utc)
    ticker = Ticker("BTCUSDT", Decimal("NaN"), Decimal("99.9"), Decimal("100.1"),
                    Decimal("1"), Decimal("1"), Decimal("1000"), Decimal("100000"),
                    Decimal("100"), Decimal("100"), now, now, now, DataStatus.AVAILABLE, {},
                    source="bitget_v3_ws", exchange="bitget")
    candles = {}
    for interval, seconds in (("5m", 300), ("15m", 900), ("1H", 3600), ("4H", 14400)):
        opened = now - timedelta(seconds=seconds)
        candles[interval] = [Candle("BTCUSDT", interval, opened, Decimal("99"), Decimal("101"),
                                    Decimal("98"), Decimal("100"), Decimal("10"), Decimal("1000"),
                                    opened, now, now, DataStatus.AVAILABLE, True, [],
                                    source="bitget_v3_ws", exchange="bitget")]
    batch = MarketDataBatch(now, [], [ticker], ("BTCUSDT",), {"BTCUSDT": candles})
    monkeypatch.setattr(runtime, "evaluate_kline_freshness", lambda *_args, **_kwargs: DataStatus.AVAILABLE)
    monkeypatch.setattr(runtime, "stage1_results", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("invalid market data must not reach Stage1")))
    config = RuntimeConfig("dsn", tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RealtimePaperMonitor(config, store=SessionStore(config.state_db), environ={})
    rows, _, _, market_ready, _ = monitor._market(batch, [], now)
    ticker_feed = next(feed for feed in rows[0]["feeds"] if feed["kind"] == "TICKER_MARK")
    assert ticker_feed["status"] == "INVALID"
    assert rows[0]["stage1"]["reason"] == "INVALID_MARKET_DATA"
    assert market_ready is False


def test_database_failure_path_keeps_live_flag_visible(tmp_path):
    store = SessionStore(tmp_path / "runtime.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT"], "data_source": "UNKNOWN"})
    config = RuntimeConfig(None, tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RealtimePaperMonitor(config, store=store,
                                   environ={"TRADING_MODE": "paper", "LIVE_ENABLED": "true"})
    monitor._session_id = session.session_id
    result = monitor.cycle(now=datetime.now(timezone.utc))
    assert result["live"] == "BLOCKED_CONFIGURATION"
    assert "live_disabled" in result["blockers"]
    assert result["final_action"] == "DO NOT TRADE"
def test_disconnect_recovers_and_existing_collector_reconnect_is_recorded(tmp_path):
    from datetime import timedelta

    class RecoveringMonitor(RealtimePaperMonitor):
        calls = 0
        reconnects = 0

        def _read_canonical(self, now):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary database/network disconnect")
            self.reconnects += 1
            health = {
                "status": "AVAILABLE", "checked_at": now,
                "details": {"ws_reconnects": self.reconnects},
            }
            return None, health, [], {"database": True}

        def _market(self, _batch, _funding_rows, _now):
            return [], [], "REAL_PUBLIC_DATA", False, []

        def _check_collector(self, _health, _now):
            return True, "COLLECTOR_HEALTHY"

        def _snapshot_strategy(self):
            return False, "PHASE9_DISABLED"

    store = SessionStore(tmp_path / "runtime.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT"], "data_source": "UNKNOWN"})
    config = RuntimeConfig(None, tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RecoveringMonitor(config, store=store, environ={"TRADING_MODE": "paper"})
    monitor._session_id = session.session_id
    base = datetime.now(timezone.utc)
    assert monitor.cycle(now=base)["data_source"] == "UNKNOWN"
    assert monitor.cycle(now=base + timedelta(seconds=1))["data_source"] == "REAL_PUBLIC_DATA"
    monitor.cycle(now=base + timedelta(seconds=2))
    events = store.readonly_snapshot(config.state_db)["events"]
    reasons = {event["reason"] for event in events}
    assert "CANONICAL_DATABASE_UNAVAILABLE" in reasons
    assert "CANONICAL_DATABASE_RECONNECTED" in reasons
    assert "EXISTING_PHASE1_COLLECTOR_RECONNECTED" in reasons
    assert [row["data_source"] for row in store.cycle_history(session.session_id)] == [
        "UNKNOWN", "REAL_PUBLIC_DATA", "REAL_PUBLIC_DATA",
    ]

def test_health_separates_process_collector_and_stale_public_ticker(tmp_path, monkeypatch):
    from datetime import timedelta
    from decimal import Decimal
    from types import SimpleNamespace
    from quant_phase1.contracts import Candle, DataStatus, Ticker
    from quant_phase1.pipeline import MarketDataBatch
    from quant_realtime_paper import runtime

    now = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
    ticker = Ticker(
        "BTCUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1"),
        Decimal("1"), Decimal("1"), Decimal("1000"), Decimal("100000"),
        Decimal("100"), Decimal("100"), now-timedelta(seconds=61),
        now-timedelta(seconds=2), now-timedelta(seconds=1), DataStatus.AVAILABLE, {},
        source="bitget_v3_ws", exchange="bitget",
    )
    candles = {}
    for interval, seconds in (("5m", 300), ("15m", 900), ("1H", 3600), ("4H", 14400)):
        opened = now-timedelta(seconds=seconds)
        candles[interval] = [Candle(
            "BTCUSDT", interval, opened, Decimal("99"), Decimal("101"), Decimal("98"),
            Decimal("100"), Decimal("10"), Decimal("1000"), opened, now, now,
            DataStatus.AVAILABLE, True, [], source="bitget_v3_ws", exchange="bitget",
        )]
    batch = MarketDataBatch(now, [], [ticker], ("BTCUSDT",), {"BTCUSDT": candles})
    funding = [{
        "symbol": "BTCUSDT", "canonical_symbol": "BTCUSDT", "exchange": "bitget",
        "exchange_timestamp": (now-timedelta(seconds=10)).isoformat(),
        "fetched_at": (now-timedelta(seconds=9)).isoformat(),
        "processed_at": (now-timedelta(seconds=8)).isoformat(),
        "status": "AVAILABLE", "normalized_8h_rate": Decimal("0.0001"),
        "next_funding_time": None,
    }]
    result = SimpleNamespace(
        symbol="BTCUSDT", category="B", classification="WAIT_TRIGGER", reason="WAIT_FOR_TRIGGER",
        status=DataStatus.AVAILABLE, reason_codes=(), structure="BULLISH", indicators={}, key_metrics={},
    )
    monkeypatch.setattr(runtime, "evaluate_kline_freshness", lambda *_a, **_k: DataStatus.AVAILABLE)
    monkeypatch.setattr(runtime, "stage1_results", lambda *_a, **_k: [result])
    store = SessionStore(tmp_path / "runtime.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT"], "data_source": "UNKNOWN"})
    config = RuntimeConfig("dsn", tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RealtimePaperMonitor(config, store=store, environ={"TRADING_MODE": "paper"})
    monitor._session_id = session.session_id
    monitor._read_canonical = lambda _now: (
        batch, {"status": "AVAILABLE", "checked_at": now}, funding,
        {"database": True, "data_type_rows": {}},
    )

    snapshot = monitor.cycle(now=now)
    health = snapshot["health"]
    assert health["process_alive"]["status"] == "ALIVE"
    assert health["collector_heartbeat"]["status"] == "AVAILABLE"
    assert health["fresh_market_data"]["status"] == "STALE"
    ticker_feed = next(feed for feed in snapshot["symbols"][0]["feeds"] if feed["data_type"] == "PRICE_STAGE1")
    assert ticker_feed["source_event_age"] == 61
    assert ticker_feed["ingest_lag"] == 59
    assert ticker_feed["processing_lag"] == 1
    assert snapshot["final_action"] == "DO NOT TRADE"


def test_cycle_checks_collector_heartbeat_at_canonical_read_completion(tmp_path, monkeypatch):
    from datetime import timedelta
    from quant_realtime_paper import runtime

    started_at = datetime.now(timezone.utc)
    read_completed_at = started_at + timedelta(seconds=1)
    clock_samples = iter((started_at, read_completed_at))
    monkeypatch.setattr(runtime, "utc_now", lambda: next(clock_samples))

    class ConcurrentHeartbeatMonitor(RealtimePaperMonitor):
        def _read_canonical(self, now):
            assert now == started_at
            return None, {
                "status": "AVAILABLE",
                "checked_at": read_completed_at,
                "details": {},
            }, [], {"database": True, "data_type_rows": {}}

        def _market(self, _batch, _funding_rows, _now):
            return [], [], "REAL_PUBLIC_DATA", False, []

        def _snapshot_strategy(self):
            return False, "PHASE9_DISABLED"

    store = SessionStore(tmp_path / "runtime.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT"], "data_source": "UNKNOWN"})
    config = RuntimeConfig("dsn", tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = ConcurrentHeartbeatMonitor(config, store=store, environ={})
    monitor._session_id = session.session_id

    snapshot = monitor.cycle()

    assert snapshot["observed_at"] == read_completed_at
    assert snapshot["health"]["collector_heartbeat"]["status"] == "AVAILABLE"


def test_funding_without_exchange_timestamp_keeps_source_age_unknown(tmp_path):
    from datetime import timedelta
    from decimal import Decimal

    now = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
    row = {
        "symbol": "BTCUSDT", "canonical_symbol": "BTCUSDT", "exchange": "bitget",
        "exchange_timestamp": None, "fetched_at": (now-timedelta(seconds=20)).isoformat(),
        "processed_at": (now-timedelta(seconds=18)).isoformat(), "status": "AVAILABLE",
        "normalized_8h_rate": Decimal("0.0001"), "next_funding_time": None,
    }
    config = RuntimeConfig("dsn", tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RealtimePaperMonitor(config, store=SessionStore(config.state_db), environ={})
    funding = monitor._phase2_funding("BTCUSDT", [row], now)
    assert funding["source_event_age"] is None
    assert funding["ingest_lag"] is None
    assert funding["processing_lag"] == 2.0
    assert funding["observation_age"] == 20.0
    assert funding["status"] == "HEALTHY"


def test_monitor_rejects_pipeline_policy_that_differs_from_runtime_approval(tmp_path):
    from quant_nautilus.acceptance import fixture_case
    from tests.quant_realtime_paper.test_execution_wiring import _setup

    runtime_case = fixture_case(
        tmp_path / "runtime-policy", mode="PAPER", pattern="TREND_CONTINUATION", code_version="b" * 40,
    )
    pipeline, _stage1, _case, _calls, _store = _setup(tmp_path / "pipeline", revision="b" * 40, pattern="BREAKOUT_CONFIRMATION")
    env = {
        "TRADING_MODE": "paper", "PAPER_ONLY": "true", "LIVE_ALLOWED": "false",
        "PHASE9_ENABLED": "1", "QUANT_BUILD_REVISION": "b" * 40,
        "PHASE9_POLICY_MANIFEST_PATH": str(tmp_path / "runtime-policy" / "manifest.json"),
        "PHASE9_POLICY_APPROVAL_PATH": str(tmp_path / "runtime-policy" / "approval.json"),
    }
    # The policy paths are generated as case files for fixture tests.
    env["PHASE9_POLICY_MANIFEST_PATH"] = str(tmp_path / "runtime-policy" / "manifest.json")
    env["PHASE9_POLICY_APPROVAL_PATH"] = str(tmp_path / "runtime-policy" / "approval.json")
    config = RuntimeConfig(None, tmp_path / "monitor.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RealtimePaperMonitor(config, store=SessionStore(config.state_db), environ=env,
                                  execution_pipeline=pipeline)

    ready, reason = monitor._snapshot_strategy()

    assert ready is False
    assert reason == "PIPELINE_APPROVAL_POLICY_MISMATCH"


@pytest.mark.parametrize(("disposition", "reason", "stage", "expected", "risk_available"), [
    ("NO_TRADE_BY_STRATEGY", "PHASE9_INSUFFICIENT", "phase9", "NO_TRADE_BY_STRATEGY", True),
    ("NO_TRADE_BY_SYSTEM_NOT_READY", "POLICY_DISABLED", "policy", "NO_TRADE_BY_POLICY_DISABLED", True),
    ("NO_TRADE_BY_SYSTEM_NOT_READY", "STALE_ACCOUNT", "risk", "NO_TRADE_BY_RISK_REJECT", True),
    ("NO_TRADE_BY_SYSTEM_NOT_READY", "STALE_MARKET_DATA", "data", "NO_TRADE_BY_STALE_DATA", True),
    ("NO_TRADE_BY_SYSTEM_NOT_READY", "INTAKE_TTL_NOT_CONFIGURED", "phase9", "NO_TRADE_BY_MISSING_TTL", True),
    ("NO_TRADE_BY_SYSTEM_NOT_READY", "PAPER_UNAVAILABLE", "paper", "NO_TRADE_BY_SYSTEM_NOT_READY", True),
    ("NO_TRADE_BY_SYSTEM_NOT_READY", "RISK_POLICY_UNAVAILABLE", "risk", "NO_TRADE_BY_RISK_REJECT", False),
])
def test_monitor_persists_precise_no_trade_class_and_pipeline_event(
    tmp_path, monkeypatch, disposition, reason, stage, expected, risk_available,
):
    from types import SimpleNamespace
    from quant_phase1.contracts import DataStatus
    from quant_execution.risk_config import RiskConfigLoader

    risk_path = tmp_path / "risk-v2.json"
    risk_path.write_text((Path(__file__).parents[2] / "config" / "risk_policy_v2.json").read_text())
    risk_loader = RiskConfigLoader(risk_path)
    if not risk_available:
        risk_path.write_text('{"schema_version":"INVALID"}')

    now = datetime.now(timezone.utc)
    stage1 = SimpleNamespace(symbol="BTCUSDT", category="A", status=DataStatus.AVAILABLE,
                             reason_codes=("STRUCTURE_ALIGNED",))
    feeds = ([{"kind": "TICKER_MARK", "status": "HEALTHY"},
              {"kind": "FUNDING", "status": "HEALTHY"}]
             + [{"kind": "KLINE", "status": "HEALTHY"} for _ in range(4)])
    market_snapshot = {"source": "bitget_v3_ws", "symbol": "BTCUSDT"}
    row = {
        "symbol": "BTCUSDT", "price": "50000", "mark_price": "50000",
        "last_update": now, "data_age_seconds": 0, "funding": {"status": "HEALTHY"},
        "feeds": feeds, "market_snapshot": market_snapshot, "features": {},
        "stage1": {"category": "A", "classification": "DEEP_ANALYSIS",
                   "reason": "ok", "reason_codes": [], "structure": "BULLISH",
                   "status": "AVAILABLE", "key_metrics": {}},
        "_stage1_result": stage1, "_input": market_snapshot,
    }
    event = {
        "correlation_id": "cycle-correlation", "stage": stage, "status": "REJECTED",
        "reason_code": reason, "at": now,
    }
    result = SimpleNamespace(
        disposition=disposition, reason_code=reason,
        decision_candidate=None, execution_intent=None, execution_results=(),
        position_snapshot=None, reconciliation="NOT_READY", events=(event,),
    )

    class Pipeline:
        event_sink = None
        calls = []

        def process_stage1(self, candidate, **kwargs):
            self.calls.append((candidate, kwargs))
            if self.event_sink:
                self.event_sink(event)
            return result

    pipeline = Pipeline()
    store = SessionStore(tmp_path / "runtime.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT"], "data_source": "UNKNOWN"})
    config = RuntimeConfig("dsn", tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path)
    monitor = RealtimePaperMonitor(
        config, store=store,
        environ={"TRADING_MODE": "paper", "PAPER_ONLY": "true", "LIVE_ALLOWED": "false"},
        execution_pipeline=pipeline,
        risk_readiness=lambda: risk_loader.poll().new_risk_allowed,
    )
    monitor._session_id = session.session_id
    monitor._read_canonical = lambda at: (
        None, {"status": "AVAILABLE", "checked_at": at, "details": {}}, [],
        {"database": True, "data_type_rows": {}},
    )
    monitor._market = lambda *_args: ([row], [stage1], "REAL_PUBLIC_DATA", True, [])
    monitor._supplemental_freshness = lambda *_args: []
    monitor._check_collector = lambda *_args: (True, "COLLECTOR_HEALTHY")
    monitor._fresh_market_data_status = lambda *_args: "HEALTHY"
    monitor._snapshot_strategy = lambda: (True, "APPROVED_POLICY_ACTIVE")

    snapshot = monitor.cycle(now=now)

    if not risk_available:
        assert pipeline.calls == []
        assert snapshot["readiness_detail"]["reason_code"] == "RISK_POLICY_UNAVAILABLE"
        assert snapshot["readiness_detail"]["risk_ready"] is False
        assert snapshot["counts"]["orders"] == 0
        return

    assert len(pipeline.calls) == 1
    assert pipeline.calls[0][1]["data_source"] == "REAL_PUBLIC_DATA"
    assert pipeline.calls[0][1]["market_data_fresh"] is True
    assert pipeline.calls[0][1]["snapshot_complete"] is True
    assert snapshot["readiness_detail"]["no_trade_classification"] == expected
    assert snapshot["symbols"][0]["no_trade_classification"] == expected
    assert snapshot["symbols"][0]["trade_semantics"] == expected
    flags = {
        "process_ready", "data_ready", "stage1_ready", "phase9_ready", "policy_ready",
        "risk_ready", "paper_ready", "reconciliation_ready", "execution_ready",
    }
    assert flags <= set(snapshot["readiness_detail"])
    persisted = store.readonly_snapshot(config.state_db)["events"]
    stored_event = next(
        item for item in persisted
        if item["details"].get("correlation_id") == "cycle-correlation"
    )
    assert stored_event["reason"] == reason
    assert stored_event["details"] == {
        "correlation_id": "cycle-correlation", "stage": stage, "status": "REJECTED",
    }
    assert datetime.fromisoformat(stored_event["event_time"]).utcoffset().total_seconds() == 0


def test_monitor_shared_v2_risk_loader_rejects_missing_invalid_and_revision_rollback(tmp_path, monkeypatch):
    from quant_execution.risk_config import RiskConfigLoader, RiskConfigV2
    from quant_realtime_paper import runtime

    path = tmp_path / "risk.json"
    loader = RiskConfigLoader(path)
    monitor = RealtimePaperMonitor(
        RuntimeConfig(None, tmp_path / "monitor.sqlite3", 15.0, ("BTCUSDT",), tmp_path),
        environ={}, risk_readiness=lambda: loader.poll().new_risk_allowed,
    )
    # A valid legacy file cannot override a rejected V2 configuration.
    monkeypatch.setattr(runtime, "_risk_policy_configured", lambda _env: True)
    assert monitor._snapshot_risk() is False
    config = RiskConfigV2(revision=2)
    path.write_text(config.model_dump_json())
    assert monitor._snapshot_risk() is True
    path.write_text(config.model_copy(update={"revision": 1}).model_dump_json())
    assert monitor._snapshot_risk() is False
    path.write_text("invalid json")
    assert monitor._snapshot_risk() is False
    path.unlink()
    assert monitor._snapshot_risk() is False
    path.write_text(config.model_copy(update={"revision": 3}).model_dump_json())
    assert monitor._snapshot_risk() is True


@pytest.mark.parametrize("value", (False, None, "true", 1))
def test_monitor_risk_readiness_does_not_accept_non_boolean_success(tmp_path, value):
    monitor = RealtimePaperMonitor(
        RuntimeConfig(None, tmp_path / "monitor.sqlite3", 15.0, ("BTCUSDT",), tmp_path),
        environ={}, risk_readiness=lambda: value,
    )
    assert monitor._snapshot_risk() is False


def test_monitor_risk_readiness_exception_blocks_new_risk(tmp_path):
    def unavailable():
        raise OSError("Risk source unavailable")
    monitor = RealtimePaperMonitor(
        RuntimeConfig(None, tmp_path / "monitor.sqlite3", 15.0, ("BTCUSDT",), tmp_path),
        environ={}, risk_readiness=unavailable,
    )
    assert monitor._snapshot_risk() is False


def test_monitor_failure_snapshot_reports_active_v2_strategy(tmp_path):
    store = SessionStore(tmp_path / "runtime.sqlite3")
    session = store.start_session({"symbols": ["BTCUSDT"], "data_source": "UNKNOWN"})
    monitor = RealtimePaperMonitor(
        RuntimeConfig(None, tmp_path / "runtime.sqlite3", 2.0, ("BTCUSDT",), tmp_path),
        store=store, environ={"TRADING_MODE": "paper", "PAPER_ONLY": "true", "LIVE_ALLOWED": "false"},
    )
    monitor._session_id = session.session_id
    result = monitor.cycle(now=datetime.now(timezone.utc))
    assert result["strategy_version"] == "QUANT_PAPER_V2"
    assert store.latest_snapshot(session.session_id)["payload"]["strategy_version"] == "QUANT_PAPER_V2"
