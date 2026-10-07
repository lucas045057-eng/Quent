"""Read-only orchestration over Phase1 canonical PostgreSQL and existing Stage1."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping

from quant_data_layer.errors import normalize_error
from quant_phase1.config import Settings
from quant_phase1.db import assert_schema_ready
from quant_phase1.pipeline import MarketDataBatch
from strategies.runtime import screen_batch, stage1_results
from quant_phase1.repositories import Phase1Repository
from quant_phase1.time import utc_now
from quant_phase1.contracts import DataStatus
from quant_phase1.stage1 import DEFAULT_GRACE_SECONDS
from quant_phase1.freshness import evaluate_kline_freshness
from quant_data_layer.freshness import (
    FRESHNESS_POLICY, assess_freshness, bitget_price_source_clock_skew_tolerance,
    kline_freshness_policy, measure_lags,
)

from .config import RuntimeConfig
from .gates import (
    ClockMonitor, assess_paper_v1_readiness, assess_readiness,
    classify_no_trade_reason, classify_public_data_source, retry_delay,
    BENIGN_NO_ENTRY_REASONS,
)
from .store import SessionStore


_INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}
# Persisted event details must stay secret-free: preflight reason codes are
# bounded constant tokens; anything else falls back to the normalized summary
# which never retains exception text.
_REASON_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


def _clean(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_clean(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _clean(asdict(value))
    return value


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else None


def _age(event_time: datetime | str | None, now: datetime) -> float | None:
    parsed = _as_datetime(event_time)
    if parsed is None:
        return None
    return round((now - parsed.astimezone(timezone.utc)).total_seconds(), 3)

def _number(value: Any) -> Decimal | None:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        return None
    return number if number.is_finite() else None


def _positive_number(value: Any) -> bool:
    number = _number(value)
    return number is not None and number > 0


def _nonnegative_number(value: Any) -> bool:
    number = _number(value)
    return number is not None and number >= 0


def _ticker_is_valid(ticker: Any) -> bool:
    prices = (ticker.last_price, ticker.bid_price, ticker.ask_price, ticker.mark_price)
    if not all(_positive_number(value) for value in prices):
        return False
    if _number(ticker.bid_price) > _number(ticker.ask_price):
        return False
    if ticker.index_price is not None and not _positive_number(ticker.index_price):
        return False
    return all(_nonnegative_number(value) for value in (
        ticker.bid_size, ticker.ask_size, ticker.volume24h, ticker.turnover24h
    ))


def _candle_is_valid(candle: Any) -> bool:
    prices = (candle.open, candle.high, candle.low, candle.close)
    if not all(_positive_number(value) for value in prices):
        return False
    open_price, high, low, close = (_number(value) for value in prices)
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        return False
    return _nonnegative_number(candle.volume) and _nonnegative_number(candle.turnover)

def _feed(kind: str, symbol: str, source: str, event_time: datetime | str | None,
          received_time: datetime | str | None, now: datetime, healthy: bool,
          *, timeframe: str | None = None, details: Mapping[str, Any] | None = None,
          invalid: bool = False, data_type: str | None = None,
          fetched_time: datetime | str | None = None,
          processed_time: datetime | str | None = None,
          policy=None, use_observation_age: bool = False,
          future_skew_tolerance_seconds: float = 0.0) -> dict[str, Any]:
    lags = measure_lags(
        now=now, source_event_time=event_time,
        fetched_at=fetched_time, processed_at=processed_time,
    )
    source_age = lags["source_event_age"]
    age = source_age if source_age is not None else (
        lags["observation_age"] if use_observation_age else None
    )
    assessment = assess_freshness(
        age, policy, future_skew_tolerance_seconds=future_skew_tolerance_seconds,
    ) if policy is not None else (
        "FRESH" if healthy and age is not None and age >= 0 else "STALE"
    )
    if invalid or assessment in {"INVALID", "INVALID_FUTURE_TIMESTAMP"}:
        status = "INVALID"
    elif assessment == "UNKNOWN":
        status = "UNKNOWN" if healthy else "STALE"
    elif assessment == "STALE":
        status = "STALE"
    elif assessment == "DEGRADED":
        status = "DEGRADED"
    else:
        status = "HEALTHY" if healthy else "STALE"
    return {
        "kind": kind,
        "data_type": data_type,
        "symbol": symbol,
        "timeframe": timeframe,
        "source": source or "UNKNOWN",
        "event_time": _clean(event_time),
        "fetched_time": _clean(fetched_time),
        "processed_time": _clean(processed_time),
        "received_time": _clean(received_time),
        "source_event_age": lags["source_event_age"],
        "ingest_lag": lags["ingest_lag"],
        "processing_lag": lags["processing_lag"],
        "observation_age": lags["observation_age"],
        "age_seconds": age,
        "status": status,
        "soft_threshold_seconds": policy.soft_seconds if policy is not None else None,
        "hard_threshold_seconds": policy.hard_seconds if policy is not None else None,
        "details": _clean(details or {}),
    }

def _git_commit(root: Path) -> str | None:
    try:
        import subprocess
        output = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True,
            text=True, timeout=2,
        )
        return output.stdout.strip()[:40]
    except Exception:
        return None


def _risk_policy_configured(env: Mapping[str, str]) -> bool:
    raw_path = env.get("QUANT_REALTIME_PAPER_RISK_POLICY_PATH", "").strip()
    if not raw_path:
        return False
    path = Path(raw_path)
    if not path.is_file():
        return False
    try:
        from quant_execution.risk import RiskPolicyV1
        payload = json.loads(path.read_text(encoding="utf-8"))
        for name in ("max_notional", "max_margin", "max_leverage", "max_risk", "max_exposure",
                     "max_reserved_risk", "max_spread_bps", "max_slippage_bps"):
            payload[name] = Decimal(str(payload[name]))
        payload["supported_decision_versions"] = tuple(payload["supported_decision_versions"])
        payload["supported_code_versions"] = tuple(payload["supported_code_versions"])
        if "supported_aux_versions" in payload:
            payload["supported_aux_versions"] = tuple(tuple(item) for item in payload["supported_aux_versions"])
        RiskPolicyV1(**payload)
        return True
    except Exception:
        return False


def _approved_strategy_configured(env: Mapping[str, str]) -> tuple[bool, str]:
    try:
        from quant_phase9.config import load_phase9_runtime_config
        config = load_phase9_runtime_config(env)
        if not config.enabled:
            return False, "PHASE9_DISABLED"
        from quant_phase9.policy import load_approved_policy_manifest
        manifest_path = Path(env.get("PHASE9_POLICY_MANIFEST_PATH", "policies/phase9_policy_v1.json"))
        approval_path = Path(env.get("PHASE9_POLICY_APPROVAL_PATH", "policies/phase9_policy_v1.approval.json"))
        expected_commit = env.get("QUANT_BUILD_REVISION", env.get("PHASE9_CODE_VERSION", ""))
        approved = load_approved_policy_manifest(
            manifest_path, approval_path, expected_commit=expected_commit,
        )
        if not approved.manifest.policy_content.enabled_patterns:
            return False, "NO_ENABLED_PATTERNS"
        return True, "APPROVED_POLICY_ACTIVE"
    except Exception as exc:
        return False, f"POLICY_UNAVAILABLE:{type(exc).__name__}"


def _live_is_disabled(env: Mapping[str, str]) -> bool:
    return (
        env.get("TRADING_MODE", "paper").strip().lower() not in {"live", "real"}
        and env.get("LIVE_ENABLED", "0").strip().lower() not in {"1", "true", "yes"}
        and env.get("LIVE_TRADING_ENABLED", "0").strip().lower() not in {"1", "true", "yes"}
    )

class RealtimePaperMonitor:
    """Monitor canonical public data and optionally delegate fail-closed Paper decisions."""

    def __init__(self, config: RuntimeConfig | None = None, *, store: SessionStore | None = None,
                 environ: Mapping[str, str] | None = None, clock: ClockMonitor | None = None,
                 execution_pipeline=None, startup_blocker: str | None = None,
                 resources: tuple[Any, ...] = (), position_manager=None,
                 risk_readiness: Callable[[], bool] | None = None) -> None:
        self.config = config or RuntimeConfig.from_env(environ)
        self.env = dict(os.environ if environ is None else environ)
        self.store = store or SessionStore(self.config.state_db)
        self.clock = clock or ClockMonitor()
        self.execution_pipeline = execution_pipeline
        self.position_manager = position_manager
        self.risk_readiness = risk_readiness
        self.startup_blocker = startup_blocker
        self._resources = list(resources)
        if (self.execution_pipeline is not None
                and getattr(self.execution_pipeline, "event_sink", None) is None):
            self.execution_pipeline.event_sink = self._record_pipeline_event
        self._database_connected: bool | None = None
        self._last_ws_reconnects: int | None = None
        self._started_monotonic = time.monotonic()

    def _snapshot_risk(self) -> bool:
        if self.risk_readiness is None:
            return _risk_policy_configured(self.env)
        try:
            # Assembly supplies its shared loader, including revision history.
            # Do not fall back to a legacy file if the V2 loader rejects risk.
            return self.risk_readiness() is True
        except Exception:
            return False

    def _record_pipeline_event(self, event: Mapping[str, Any]) -> None:
        session_id = getattr(self, "_session_id", None)
        if not session_id:
            return
        self.store.record_event(
            session_id, "PIPELINE", str(event.get("reason_code", "PIPELINE_EVENT")),
            {"correlation_id": event.get("correlation_id"),
             "stage": event.get("stage"), "status": event.get("status")},
            now=event.get("at") if isinstance(event.get("at"), datetime) else None,
        )

    def _read_canonical(self, now: datetime) -> tuple[MarketDataBatch | None, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        if not self.config.dsn:
            raise RuntimeError("QUANT_REALTIME_PAPER_DSN_REQUIRED")
        import psycopg
        with psycopg.connect(
            self.config.dsn, connect_timeout=3, autocommit=True,
            options="-c default_transaction_read_only=on -c statement_timeout=4000 -c lock_timeout=1000",
        ) as connection:
            if connection.execute("SELECT 1 AS ok").fetchone()[0] != 1:
                raise RuntimeError("DATABASE_PROBE_FAILED")
            assert_schema_ready(connection)
            repository = Phase1Repository(connection)
            settings = Settings.from_env(self.env)
            # Paper readiness and screening must inspect the configured universe,
            # not only Phase1's default top-200 batch.
            batch = repository.load_latest_market_batch(
                limit=max(settings.universe_limit, len(self.config.symbols)),
                candle_limit=settings.kline_fetch_limit,
                requested_symbols=self.config.symbols,
            )
            self._v2_screening = screen_batch(batch, now=now, connection=connection) if batch else None
            health_row = connection.execute(
                "SELECT status,checked_at,details FROM system_health WHERE component='quant-collector'"
            ).fetchone()
            health = dict(zip(("status", "checked_at", "details"), health_row, strict=True)) if health_row else {}
            funding_rows: list[dict[str, Any]] = []
            try:
                fetched_funding_rows = connection.execute(
                    """SELECT DISTINCT ON (COALESCE(canonical_symbol,symbol))
                              symbol,canonical_symbol,exchange,exchange_timestamp,fetched_at,processed_at,
                              status,normalized_8h_rate,next_funding_time
                       FROM funding_rates WHERE exchange='bitget'
                         AND (symbol=ANY(%s) OR canonical_symbol=ANY(%s))
                       ORDER BY COALESCE(canonical_symbol,symbol),processed_at DESC LIMIT %s""",
                    (list(self.config.symbols), list(self.config.symbols), len(self.config.symbols)),
                ).fetchall()
                columns = ("symbol", "canonical_symbol", "exchange", "exchange_timestamp", "fetched_at",
                           "processed_at", "status", "normalized_8h_rate", "next_funding_time")
                funding_rows = [dict(zip(columns, row, strict=True)) for row in fetched_funding_rows]
            except psycopg.Error:
                funding_rows = []
            data_type_rows: dict[str, list[dict[str, Any]]] = {
                "open_interest": [], "trade_flow": [], "liquidation": [],
            }
            canonical_symbols = tuple(
                f"{symbol[:-4]}-USDT-PERP" if symbol.endswith("USDT") else symbol
                for symbol in self.config.symbols
            )
            optional_queries = {
                "open_interest": (
                    """SELECT DISTINCT ON (COALESCE(canonical_symbol,symbol))
                              symbol,canonical_symbol,exchange,exchange_timestamp,fetched_at,processed_at,status
                         FROM open_interest WHERE exchange='bitget'
                           AND (symbol=ANY(%s) OR canonical_symbol=ANY(%s))
                         ORDER BY COALESCE(canonical_symbol,symbol),processed_at DESC LIMIT 20""",
                    (list(self.config.symbols), list(canonical_symbols)),
                ),
                "trade_flow": (
                    """SELECT DISTINCT ON (canonical_symbol,timeframe)
                              exchange,canonical_symbol,timeframe,window_close,last_trade_at,
                              processed_at,status,freshness
                         FROM trade_flow_windows WHERE exchange='bitget' AND canonical_symbol=ANY(%s)
                           AND window_close <= %s AND status='AVAILABLE' AND freshness='AVAILABLE'
                         ORDER BY canonical_symbol,timeframe,window_close DESC LIMIT 20""",
                    (list(canonical_symbols), now),
                ),
                "liquidation": (
                    """SELECT DISTINCT ON (canonical_symbol)
                              exchange,canonical_symbol,event_timestamp,received_at,processed_at,status
                         FROM liquidation_events WHERE exchange='bitget' AND canonical_symbol=ANY(%s)
                         ORDER BY canonical_symbol,event_timestamp DESC LIMIT 20""",
                    (list(canonical_symbols),),
                ),
            }
            optional_columns = {
                "open_interest": ("symbol", "canonical_symbol", "exchange", "exchange_timestamp",
                                  "fetched_at", "processed_at", "status"),
                "trade_flow": ("exchange", "canonical_symbol", "timeframe", "window_close",
                               "last_trade_at", "processed_at", "status", "freshness"),
                "liquidation": ("exchange", "canonical_symbol", "event_timestamp", "received_at",
                                "processed_at", "status"),
            }
            for name, (statement, params) in optional_queries.items():
                try:
                    cursor = connection.execute(statement, params)
                    data_type_rows[name] = [
                        dict(zip(optional_columns[name], row, strict=True))
                        for row in cursor.fetchall()
                    ]
                except psycopg.Error:
                    # Optional canonical Phase 2-4 sources remain visible as unavailable.
                    data_type_rows[name] = []
        return batch, _clean(health), [_clean(row) for row in funding_rows], {
            "database": True, "data_type_rows": _clean(data_type_rows),
        }

    def _configured_policy(self, data_type: str, soft_seconds: float, hard_seconds: float):
        base = FRESHNESS_POLICY[data_type]
        hard = min(float(hard_seconds), base.hard_seconds)
        soft = min(float(soft_seconds), base.soft_seconds, hard)
        return replace(base, soft_seconds=soft, hard_seconds=hard)

    def _phase2_funding(self, symbol: str, rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
        matches = [row for row in rows if row.get("symbol") == symbol or row.get("canonical_symbol") == symbol]
        if not matches:
            return {"event_time": None, "received_time": None, "rate": None, "status": "NOT_AVAILABLE",
                    "source": "phase2.funding_rates", "exchange": None, "age_seconds": None}
        row = max(matches, key=lambda item: str(item.get("processed_at") or ""))
        event_time = row.get("exchange_timestamp")
        received_time = row.get("processed_at") or row.get("fetched_at")
        fetched_at = row.get("fetched_at")
        processed_at = row.get("processed_at") or fetched_at
        lags = measure_lags(
            now=now, source_event_time=event_time,
            fetched_at=fetched_at, processed_at=processed_at,
        )
        assessment_age = lags["source_event_age"]
        if assessment_age is None:
            assessment_age = lags["observation_age"]
        funding_policy = self._configured_policy(
            "FUNDING", self.config.funding_soft_max_age_seconds,
            self.config.funding_max_age_seconds,
        )
        assessment = assess_freshness(assessment_age, funding_policy)
        if row.get("status") != "AVAILABLE":
            status = "STALE"
        else:
            status = {
                "FRESH": "HEALTHY", "DEGRADED": "DEGRADED", "STALE": "STALE",
                "INVALID_FUTURE_TIMESTAMP": "INVALID", "INVALID": "INVALID", "UNKNOWN": "UNKNOWN",
            }[assessment]
        return {
            "event_time": event_time,
            "received_time": received_time,
            "fetched_at": fetched_at,
            "processed_at": processed_at,
            "source_event_age": lags["source_event_age"],
            "ingest_lag": lags["ingest_lag"],
            "processing_lag": lags["processing_lag"],
            "observation_age": lags["observation_age"],
            "age_seconds": assessment_age,
            "rate": row.get("normalized_8h_rate"),
            "next_funding_time": row.get("next_funding_time"),
            "exchange": row.get("exchange"),
            "source": f"phase2:{row.get('exchange')}",
            "status": status,
            "event_time_provided_by_source": event_time is not None,
        }

    def _supplemental_freshness(self, data_type_rows: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        for row in data_type_rows.get("open_interest", ()):
            event_time = row.get("exchange_timestamp")
            fetched_at, processed_at = row.get("fetched_at"), row.get("processed_at")
            feed = _feed(
                "OPEN_INTEREST", str(row.get("canonical_symbol") or row.get("symbol") or "UNKNOWN"),
                "phase2:bitget", event_time, processed_at or fetched_at, now,
                row.get("status") == "AVAILABLE", data_type="OPEN_INTEREST",
                fetched_time=fetched_at, processed_time=processed_at,
                policy=FRESHNESS_POLICY["OPEN_INTEREST"], use_observation_age=True,
            )
            if row.get("status") != "AVAILABLE" and feed["status"] == "HEALTHY":
                feed["status"] = "STALE"
            diagnostics.append(feed)

        for row in data_type_rows.get("trade_flow", ()):
            timeframe = str(row.get("timeframe") or "UNKNOWN")
            interval_seconds = _INTERVAL_SECONDS.get(timeframe, 300)
            policy = FRESHNESS_POLICY["TRADE_FLOW"]
            event_time = row.get("last_trade_at")
            processed_at = row.get("processed_at")
            feed = _feed(
                "TRADE_FLOW", str(row.get("canonical_symbol") or "UNKNOWN"),
                "phase3:trade_flow_windows", event_time, processed_at, now,
                row.get("status") in {"AVAILABLE", "PARTIAL"}, timeframe=timeframe,
                data_type="TRADE_FLOW", processed_time=processed_at, policy=None,
            )
            window_age = _age(row.get("window_close"), now)
            window_policy = type(policy)(
                policy.data_type, f"{timeframe} aggregate window", interval_seconds,
                min(policy.soft_seconds, float(interval_seconds)),
                float(interval_seconds + 5),
                "Window-close age governs freshness; a quiet last-trade timestamp is not itself a gap.",
            )
            state = assess_freshness(window_age, window_policy)
            if row.get("status") not in {"AVAILABLE", "PARTIAL"} or row.get("freshness") == "STALE":
                state = "STALE"
            feed.update({
                "status": {"FRESH": "HEALTHY", "DEGRADED": "DEGRADED", "STALE": "STALE",
                           "UNKNOWN": "UNKNOWN", "INVALID_FUTURE_TIMESTAMP": "INVALID",
                           "INVALID": "INVALID"}.get(state, "UNKNOWN"),
                "soft_threshold_seconds": window_policy.soft_seconds,
                "hard_threshold_seconds": window_policy.hard_seconds,
                "window_close": _clean(row.get("window_close")),
                "window_age_seconds": window_age,
            })
            diagnostics.append(feed)

        for row in data_type_rows.get("liquidation", ()):
            event_time, received_at, processed_at = (
                row.get("event_timestamp"), row.get("received_at"), row.get("processed_at")
            )
            feed = _feed(
                "LIQUIDATION", str(row.get("canonical_symbol") or "UNKNOWN"),
                "phase4:liquidation_events", event_time, received_at, now,
                row.get("status") == "AVAILABLE", data_type="LIQUIDATION",
                fetched_time=received_at, processed_time=processed_at,
                policy=None,
            )
            event_lag = measure_lags(
                now=now, source_event_time=event_time,
                fetched_at=received_at, processed_at=processed_at,
            )["source_event_age"]
            delivery_lag = measure_lags(
                now=processed_at or now, source_event_time=event_time,
                fetched_at=received_at, processed_at=processed_at,
            )["source_event_age"]
            state = assess_freshness(delivery_lag, FRESHNESS_POLICY["LIQUIDATION"])
            feed.update({
                "status": {"FRESH": "HEALTHY", "DEGRADED": "DEGRADED", "STALE": "STALE",
                           "UNKNOWN": "UNKNOWN", "INVALID_FUTURE_TIMESTAMP": "INVALID",
                           "INVALID": "INVALID"}.get(state, "UNKNOWN"),
                "soft_threshold_seconds": FRESHNESS_POLICY["LIQUIDATION"].soft_seconds,
                "hard_threshold_seconds": FRESHNESS_POLICY["LIQUIDATION"].hard_seconds,
                "event_silence_seconds": event_lag,
                "event_silence_is_stale": False,
            })
            diagnostics.append(feed)

        missing = {
            "open_interest": ("OPEN_INTEREST", "No selected-symbol observation in canonical Phase2 open_interest."),
            "trade_flow": ("TRADE_FLOW", "No closed AVAILABLE selected-symbol window in canonical Phase3 trade_flow_windows; open/PARTIAL windows are excluded."),
            "liquidation": ("LIQUIDATION", "No recent canonical event; event silence alone is not stale."),
        }
        for source_name, (data_type, reason) in missing.items():
            if data_type_rows.get(source_name):
                continue
            policy = FRESHNESS_POLICY[data_type]
            diagnostics.append({
                "data_type": data_type,
                "symbol": None,
                "source": "phase2/3/4 canonical tables",
                "status": "NO_EVENT_SAMPLE" if data_type == "LIQUIDATION" else "NOT_AVAILABLE",
                "source_event_age": None,
                "ingest_lag": None,
                "processing_lag": None,
                "observation_age": None,
                "soft_threshold_seconds": policy.soft_seconds,
                "hard_threshold_seconds": policy.hard_seconds,
                "reason": reason,
            })
        return diagnostics

    @staticmethod
    def _fresh_market_data_status(rows: list[dict[str, Any]]) -> str:
        critical = [
            feed for row in rows for feed in row.get("feeds", ())
            if feed.get("data_type") in {"PRICE_STAGE1", "PRICE_DECISION", "FUNDING"}
        ]
        if not critical:
            return "UNKNOWN"
        statuses = {feed.get("status") for feed in critical}
        if statuses & {"STALE", "INVALID", "UNKNOWN"}:
            return "STALE"
        if "DEGRADED" in statuses:
            return "DEGRADED"
        return "FRESH"

    def _check_collector(self, health: Mapping[str, Any], now: datetime) -> tuple[bool, str]:
        checked = health.get("checked_at")
        if isinstance(checked, str):
            try:
                checked = datetime.fromisoformat(checked.replace("Z", "+00:00"))
            except ValueError:
                checked = None
        if not isinstance(checked, datetime) or checked.tzinfo is None:
            return False, "COLLECTOR_HEARTBEAT_MISSING"
        age = (now - checked).total_seconds()
        healthy = health.get("status") == "AVAILABLE" and 0 <= age <= self.config.collector_max_age_seconds
        return healthy, "COLLECTOR_HEALTHY" if healthy else "COLLECTOR_HEARTBEAT_STALE"

    def _market(self, batch: MarketDataBatch | None, funding_rows: list[dict[str, Any]], now: datetime):
        if batch is None:
            return [], [], "UNKNOWN", False, []
        ticker_policy = self._configured_policy(
            "PRICE_STAGE1", self.config.ticker_soft_max_age_seconds,
            self.config.ticker_max_age_seconds,
        )
        selected = [symbol for symbol in self.config.symbols if symbol in batch.selected_symbols]
        tickers = [ticker for ticker in batch.tickers if ticker.symbol in selected]
        candles = {symbol: batch.candles_by_symbol.get(symbol, {}) for symbol in selected}
        all_tickers = {ticker.symbol: ticker for ticker in tickers}
        valid_selected = [
            symbol for symbol in selected
            if symbol in all_tickers
            and _ticker_is_valid(all_tickers[symbol])
            and all(_candle_is_valid(bar)
                    for bars in candles.get(symbol, {}).values() for bar in bars)
        ]
        subset = replace(
            batch,
            tickers=[all_tickers[symbol] for symbol in valid_selected],
            selected_symbols=tuple(valid_selected),
            candles_by_symbol={symbol: candles[symbol] for symbol in valid_selected},
        )
        results = stage1_results(getattr(self,"_v2_screening",None) or screen_batch(subset, now=now)) if valid_selected else []
        observations: list[dict[str, object]] = []
        output: list[dict[str, Any]] = []
        by_ticker = all_tickers
        result_by_symbol = {result.symbol: result for result in results}
        all_market_ready = len(selected) == len(self.config.symbols)
        for symbol in self.config.symbols:
            source_offset = len(observations)
            ticker = by_ticker.get(symbol)
            result = result_by_symbol.get(symbol)
            symbol_candles = candles.get(symbol, {})
            feeds: list[dict[str, Any]] = []
            market_snapshot: dict[str, Any] = {"symbol": symbol}
            invalid_market_data = False
            if ticker is None:
                all_market_ready = False
                feeds.append(_feed(
                    "TICKER", symbol, "phase1.market_snapshots", None, None, now, False,
                    data_type="PRICE_STAGE1", policy=FRESHNESS_POLICY["PRICE_STAGE1"],
                ))
            else:
                observations.append({"exchange": ticker.exchange, "source": ticker.source})
                ticker_invalid = not _ticker_is_valid(ticker)
                invalid_market_data |= ticker_invalid
                age = _age(ticker.exchange_timestamp, now)
                healthy = not ticker_invalid and ticker.status is DataStatus.AVAILABLE
                feeds.append(_feed(
                    "TICKER_MARK", symbol, ticker.source, ticker.exchange_timestamp,
                    ticker.processed_at, now, healthy, invalid=ticker_invalid,
                    data_type="PRICE_STAGE1", fetched_time=ticker.fetched_at,
                    processed_time=ticker.processed_at, policy=ticker_policy,
                    future_skew_tolerance_seconds=bitget_price_source_clock_skew_tolerance(
                        "PRICE_STAGE1", ticker.exchange, ticker.source,
                    ),
                    details={"exchange": ticker.exchange, "mark_price": ticker.mark_price},
                ))
                market_snapshot.update({
                    "source": ticker.source, "exchange": ticker.exchange,
                    "last_price": ticker.last_price, "mark_price": ticker.mark_price,
                    "bid_price": ticker.bid_price, "ask_price": ticker.ask_price,
                    "turnover24h": ticker.turnover24h, "event_time": ticker.exchange_timestamp,
                    "received_time": ticker.processed_at,
                })
            for interval in ("5m", "15m", "1H", "4H"):
                bars = symbol_candles.get(interval, [])
                latest = bars[-1] if bars else None
                if latest is None:
                    feeds.append(_feed(
                        "KLINE", symbol, "phase1.klines", None, None, now, False,
                        timeframe=interval, data_type="PRICE_DECISION",
                        policy=kline_freshness_policy(
                            interval, _INTERVAL_SECONDS[interval], DEFAULT_GRACE_SECONDS[interval],
                        ),
                    ))
                    all_market_ready = False
                    continue
                observations.append({"exchange": latest.exchange, "source": latest.source})
                interval_invalid = any(not _candle_is_valid(bar) for bar in bars)
                invalid_market_data |= interval_invalid
                event_time = latest.bar_open_timestamp + timedelta(seconds=_INTERVAL_SECONDS[interval])
                freshness = evaluate_kline_freshness(
                    now, interval, latest.bar_open_timestamp, grace_seconds=DEFAULT_GRACE_SECONDS[interval]
                )
                healthy = (not interval_invalid and latest.status is DataStatus.AVAILABLE
                           and freshness is DataStatus.AVAILABLE)
                kline_policy = kline_freshness_policy(
                    interval, _INTERVAL_SECONDS[interval], DEFAULT_GRACE_SECONDS[interval],
                )
                feeds.append(_feed(
                    "KLINE", symbol, latest.source, event_time, latest.processed_at,
                    now, healthy, timeframe=interval, invalid=interval_invalid,
                    data_type="PRICE_DECISION", fetched_time=latest.fetched_at,
                    processed_time=latest.processed_at, policy=kline_policy,
                    details={"bar_open_time": latest.bar_open_timestamp, "closed": latest.is_closed},
                ))
                market_snapshot[f"kline_{interval}"] = {
                    "event_time": event_time, "received_time": latest.processed_at,
                    "open": latest.open, "high": latest.high, "low": latest.low, "close": latest.close,
                    "volume": latest.volume, "turnover": latest.turnover, "source": latest.source,
                    "exchange": latest.exchange,
                }
            funding = self._phase2_funding(symbol, funding_rows, now)
            observations.append({"exchange": funding.get("exchange"),
                                 "source": funding.get("source") or "phase2.funding_rates"})
            funding_age = funding.get("age_seconds")
            funding_invalid = isinstance(funding_age, (int, float)) and funding_age < 0
            feeds.append(_feed(
                "FUNDING", symbol, str(funding.get("source") or "phase2.funding_rates"),
                funding.get("event_time"), funding.get("received_time"), now,
                funding.get("status") == "HEALTHY", invalid=funding_invalid,
                details={"rate": funding.get("rate"), "next_funding_time": funding.get("next_funding_time")},
                data_type="FUNDING", fetched_time=funding.get("fetched_at"),
                processed_time=funding.get("processed_at"),
                policy=self._configured_policy(
                    "FUNDING", self.config.funding_soft_max_age_seconds,
                    self.config.funding_max_age_seconds,
                ),
                use_observation_age=True,
            ))
            # Universe base-market readiness asserts that the shared observation
            # plumbing covers the configured universe with structurally valid
            # data. Per-symbol timeliness of ticker, kline and funding evidence is
            # a per-symbol hard-data outcome: it is enforced below before that
            # symbol may enter the pipeline, and it must not fail the whole
            # observation loop. Demanding that all 478 symbols' newest closed bar
            # has landed inside its grace window is a full-market hard-data claim,
            # which the approved run-readiness semantics exclude.
            if invalid_market_data:
                all_market_ready = False
            stage1_reason = (
                result.reason if result else
                ("INVALID_MARKET_DATA" if invalid_market_data else "CANONICAL_DATA_UNAVAILABLE")
            )
            output.append({
                "symbol": symbol,
                "price": str(ticker.last_price) if ticker else None,
                "mark_price": str(ticker.mark_price) if ticker and ticker.mark_price is not None else None,
                "last_update": _clean(ticker.processed_at) if ticker else None,
                "data_age_seconds": _age(ticker.exchange_timestamp, now) if ticker else None,
                "funding": funding,
                "feeds": feeds,
                "market_snapshot": market_snapshot,
                "features": _clean(result.indicators) if result else {},
                "stage1": {
                    "category": result.category if result else "D",
                    "classification": result.classification if result else "REJECT",
                    "reason": stage1_reason,
                    "reason_codes": list(result.reason_codes) if result else [stage1_reason],
                    "structure": result.structure if result else None,
                    "status": result.status.value if result else ("ERROR" if invalid_market_data else "NOT_AVAILABLE"),
                    "key_metrics": _clean(result.key_metrics) if result else {},
                },
                "_stage1_result": result,
                "_input": market_snapshot,
                "_source_class": (classify_public_data_source(observations[source_offset:])
                                  if len(observations) - source_offset == 6 else "UNKNOWN"),
            })
        source_class = classify_public_data_source(observations)
        real_data = source_class == "REAL_PUBLIC_DATA" and len(observations) >= len(self.config.symbols) * 5
        if not real_data:
            all_market_ready = False
        return output, results, source_class, all_market_ready, observations
    def _snapshot_strategy(self) -> tuple[bool, str]:
        if self.startup_blocker:
            return False, self.startup_blocker
        ready, reason = _approved_strategy_configured(self.env)
        if not ready or self.execution_pipeline is None:
            return ready, reason
        try:
            from quant_phase9.canonical import canonical_sha256
            from quant_phase9.policy import load_approved_policy_manifest
            expected_commit = self.env.get("QUANT_BUILD_REVISION", self.env.get("PHASE9_CODE_VERSION", ""))
            manifest_path = Path(self.env.get("PHASE9_POLICY_MANIFEST_PATH", "policies/phase9_policy_v1.json"))
            approval_path = Path(self.env.get("PHASE9_POLICY_APPROVAL_PATH", "policies/phase9_policy_v1.approval.json"))
            runtime_policy = load_approved_policy_manifest(
                manifest_path, approval_path, expected_commit=expected_commit,
            )
            pipeline_policy = getattr(self.execution_pipeline, "approved_policy", None)
            if pipeline_policy is None:
                return False, "PIPELINE_APPROVAL_MISSING"
            if (getattr(self.execution_pipeline, "current_revision", None) != expected_commit
                    or pipeline_policy.code_version != expected_commit
                    or pipeline_policy.approval.approved_commit != expected_commit
                    or pipeline_policy.manifest_version != runtime_policy.manifest_version
                    or pipeline_policy.manifest_digest != runtime_policy.manifest_digest
                    or canonical_sha256(pipeline_policy.approval.model_dump(mode="python"))
                    != canonical_sha256(runtime_policy.approval.model_dump(mode="python"))):
                return False, "PIPELINE_APPROVAL_POLICY_MISMATCH"
        except Exception as exc:
            return False, f"PIPELINE_POLICY_UNAVAILABLE:{type(exc).__name__}"
        return True, reason

    def cycle(self, *, now: datetime | None = None) -> dict[str, Any]:
        at = now or utc_now()
        wall_clock_ok = self.clock.observe(at, time.monotonic())
        try:
            batch, collector_health, funding_rows, database_state = self._read_canonical(at)
            if now is None:
                # The collector can commit a newer heartbeat while this read is
                # in flight. Evaluate all freshness at read completion so that
                # a fresh concurrent write is not mistaken for future data.
                at = max(at, utc_now())
            connected = True
            if self._database_connected is False:
                self.store.record_event(self._session_id, "RECOVERED", "CANONICAL_DATABASE_RECONNECTED")
            self._database_connected = connected
            rows, results, source_class, market_ready, _ = self._market(batch, funding_rows, at)
            supplemental_freshness = self._supplemental_freshness(
                database_state.get("data_type_rows", {}), at,
            )
            collector_ready, collector_reason = self._check_collector(collector_health, at)
            fresh_market_data_status = self._fresh_market_data_status(rows)
            strategy_ready, strategy_reason = self._snapshot_strategy()
            live_disabled = _live_is_disabled(self.env)
            risk_ready = self._snapshot_risk()
            paper_mode_locked = (
                self.env.get("TRADING_MODE", "").strip().lower() == "paper"
                and self.env.get("PAPER_ONLY", "").strip().lower() == "true"
                and self.env.get("LIVE_ALLOWED", "").strip().lower() == "false"
            )
            position_management_ready = True
            manager=self.position_manager
            if manager is None and self.execution_pipeline is not None:
                manager=getattr(self.execution_pipeline,"manage_positions",None)
            if manager is not None:
                try:
                    manager(now=at)
                except Exception as error:
                    position_management_ready = False
                    self.store.record_event(self._session_id,"POSITION_MANAGEMENT","POSITION_MANAGEMENT_UNRESOLVED",
                        {"exception_type":type(error).__name__},now=at)
            upstream_ready = bool(
                position_management_ready
                and collector_ready and wall_clock_ok and strategy_ready
                and risk_ready and live_disabled and paper_mode_locked
                and self.execution_pipeline is not None and self._session_id
            )
            pipeline_results = []
            pipeline_exception = False
            for row in rows:
                stage1_result = row.get("_stage1_result")
                required_feeds = [feed for feed in row["feeds"]
                                  if feed["kind"] in {"TICKER_MARK", "KLINE", "FUNDING"}]
                symbol_fresh = bool(required_feeds) and all(feed["status"] == "HEALTHY" for feed in required_feeds)
                snapshot_complete = (
                    row.get("market_snapshot", {}).get("source") is not None
                    and len([feed for feed in required_feeds if feed["kind"] == "KLINE"]) == 4
                    and len([feed for feed in required_feeds if feed["kind"] == "TICKER_MARK"]) == 1
                )
                outcome = None
                symbol_source = row.get("_source_class", source_class)
                if (upstream_ready and stage1_result is not None
                        and symbol_source == "REAL_PUBLIC_DATA"
                        and symbol_fresh and snapshot_complete
                        and stage1_result.status is DataStatus.AVAILABLE):
                    try:
                        outcome = self.execution_pipeline.process_stage1(
                            stage1_result, data_source=symbol_source,
                            market_data_fresh=symbol_fresh, snapshot_complete=snapshot_complete,
                            now=at,
                        )
                        if outcome is None:
                            raise ValueError("PIPELINE_OUTCOME_MISSING")
                    except Exception as pipeline_error:
                        pipeline_exception = True
                        self.store.record_event(
                            self._session_id, "PIPELINE_ERROR", "RUNTIME_PIPELINE_FAILED",
                            {"exception_type": type(pipeline_error).__name__}, now=at,
                        )
                row["_pipeline_result"] = outcome
                if outcome is not None:
                    pipeline_results.append(outcome)
            wired_results = bool(pipeline_results)
            checks = {
                "market_data": market_ready and collector_ready,
                "clock": wall_clock_ok,
                "database": True,
                "collector": collector_ready,
                "strategy": strategy_ready,
                "candidate_validity": wired_results and any(
                    result.decision_candidate is not None and result.decision_candidate.eligible
                    for result in pipeline_results
                ),
                "risk": risk_ready,
                "paper_engine": wired_results and all(
                    any(event["reason_code"] == "PAPER_PREFLIGHT_PASSED" for event in result.events)
                    or result.disposition == "PAPER_RESULT_RECORDED"
                    for result in pipeline_results
                ),
                "reconciliation": wired_results and all(
                    result.reconciliation == "RECONCILED" for result in pipeline_results
                ),
                "live_disabled": live_disabled and paper_mode_locked,
                "execution_wired": self.execution_pipeline is not None,
                "paper_only": paper_mode_locked,
            }
            # Operational readiness is independently proven; an absent entry
            # candidate cannot by itself make a healthy observation loop fail.
            # Legacy/custom pipelines without this real preflight stay closed.
            operational_checks=None
            operational_probe={}
            candidate_pipeline_failed=False
            if getattr(self.execution_pipeline,"operational_preflight",None) is not None:
                try:
                    operational_probe=self.execution_pipeline.operational_readiness(now=utc_now())
                except Exception as error:
                    _message = str(error)
                    self.store.record_event(self._session_id,"ERROR","PAPER_OPERATIONAL_PREFLIGHT_FAILED",
                        {"exception_type":type(error).__name__,
                         "error":_message if isinstance(error,ValueError) and _REASON_TOKEN.fullmatch(_message)
                                 else normalize_error(error).safe_summary},now=at)
                candidate_pipeline_failed=pipeline_exception or any(
                    result.disposition!='PAPER_RESULT_RECORDED'
                    and result.reason_code not in BENIGN_NO_ENTRY_REASONS
                    for result in pipeline_results)
                operational_checks={name:value for name,value in checks.items()
                    if name not in {'candidate_validity','paper_engine','reconciliation'}}
                operational_checks.update(
                    real_public_source=source_class == 'REAL_PUBLIC_DATA',
                    position_management=position_management_ready,
                    candidate_pipeline=not candidate_pipeline_failed,
                    paper_engine=operational_probe.get('paper_engine') is True,
                    reconciliation=operational_probe.get('reconciliation') is True)
            gate = assess_readiness(operational_checks if operational_checks is not None else checks)
            readiness_checks = {
                "process_ready": True,
                "data_ready": bool(market_ready and collector_ready and wall_clock_ok
                                   and source_class == "REAL_PUBLIC_DATA"),
                "stage1_ready": bool(results) and all(
                    getattr(result, "status", None) is DataStatus.AVAILABLE for result in results
                ),
                "phase9_ready": any(
                    any(event.get("stage") in {"phase9", "evaluation_snapshot"}
                        for event in result.events) for result in pipeline_results
                ),
                "policy_ready": strategy_ready,
                "risk_ready": risk_ready,
                "paper_ready": checks["paper_engine"],
                "reconciliation_ready": checks["reconciliation"],
                "execution_ready": checks["execution_wired"] and checks["paper_only"] and checks["live_disabled"],
            }
            failure_stage = "system"
            readiness_reason = "RUNTIME_PIPELINE_NOT_CONFIGURED"
            if not readiness_checks["data_ready"]:
                failure_stage = "data"
                readiness_reason = ("STALE_MARKET_DATA" if fresh_market_data_status != "HEALTHY"
                                    or source_class != "REAL_PUBLIC_DATA" else collector_reason)
            elif not strategy_ready:
                failure_stage = "policy"
                readiness_reason = strategy_reason.split(":", 1)[0]
            elif pipeline_results:
                for result in pipeline_results:
                    if result.disposition not in {"PAPER_RESULT_RECORDED"}:
                        readiness_reason = result.reason_code
                        readiness_stage = next((event.get("stage") for event in reversed(result.events)
                                                if event.get("reason_code") == result.reason_code), None)
                        failure_stage = readiness_stage or "system"
                        break
            elif not risk_ready:
                failure_stage, readiness_reason = "risk", "RISK_POLICY_UNAVAILABLE"
            readiness_detail = asdict(assess_paper_v1_readiness(
                readiness_checks, failure_stage=failure_stage, reason_code=readiness_reason,
            ))
            if operational_checks is not None:
                # A recorded candidate must prove its own complete original
                # pipeline. Other observation-only symbols cannot supply proof.
                entry_ready = gate.status == 'PAPER READY' and any(
                    result.disposition == 'PAPER_RESULT_RECORDED'
                    and result.decision_candidate is not None
                    and result.decision_candidate.eligible
                    and result.execution_intent is not None
                    and result.reconciliation == 'RECONCILED'
                    for result in pipeline_results)
                readiness_detail.update(
                    operational_ready=gate.status=='PAPER READY',
                    entry_ready=entry_ready,
                    candidate_validity=checks['candidate_validity'],
                    legacy_entry_checks=readiness_detail.copy(),
                    entry_blockers=() if entry_ready else readiness_detail['blockers'],
                    operational_checks=operational_checks,
                    status=gate.status,final_action=gate.final_action,blockers=gate.blockers)
                readiness_detail['execution_ready'] = entry_ready
                if entry_ready:
                    readiness_detail.update(no_trade_classification='PAPER_READY',
                        reason_code='PAPER_EXECUTION_READY')
                elif gate.status=='PAPER READY':
                    readiness_detail.update(no_trade_classification='NO_TRADE_BY_STRATEGY',
                        reason_code='OPERATIONAL_READY_NO_ELIGIBLE_ENTRY')
                elif not operational_probe:
                    readiness_detail.update(no_trade_classification='NO_TRADE_BY_SYSTEM_NOT_READY',
                        reason_code='PAPER_OPERATIONAL_PREFLIGHT_FAILED')
                elif pipeline_exception:
                    readiness_detail.update(no_trade_classification='NO_TRADE_BY_SYSTEM_NOT_READY',
                        reason_code='RUNTIME_PIPELINE_FAILED')
            if not live_disabled:
                self.store.record_event(self._session_id, "ERROR", "LIVE_MODE_CONFIGURATION_REJECTED")
            ws_reconnects = collector_health.get("details", {}).get("ws_reconnects") if isinstance(collector_health.get("details"), dict) else None
            if isinstance(ws_reconnects, int):
                if self._last_ws_reconnects is not None and ws_reconnects > self._last_ws_reconnects:
                    self.store.record_event(self._session_id, "RECONNECT", "EXISTING_PHASE1_COLLECTOR_RECONNECTED",
                                            {"reconnect_count": ws_reconnects})
                self._last_ws_reconnects = ws_reconnects
            for row in rows:
                result = row.pop("_stage1_result")
                market_snapshot = row.pop("_input")
                outcome = row.pop("_pipeline_result", None)
                stage1 = row["stage1"]
                strategy_no_trade = outcome is not None and outcome.disposition == "NO_TRADE_BY_STRATEGY"
                execution_recorded = outcome is not None and outcome.disposition == "PAPER_RESULT_RECORDED"
                if execution_recorded:
                    no_trade_classification = None
                elif outcome is None:
                    no_trade_classification = readiness_detail["no_trade_classification"]
                elif strategy_no_trade:
                    no_trade_classification = "NO_TRADE_BY_STRATEGY"
                else:
                    failed_event = next((event for event in reversed(outcome.events)
                                         if event.get("reason_code") == outcome.reason_code), None)
                    failure_stage = failed_event.get("stage") if failed_event else None
                    no_trade_classification = classify_no_trade_reason(
                        failure_stage, outcome.reason_code,
                    )
                payload = {
                    "timestamp": at,
                    "symbol": row["symbol"],
                    "market_snapshot": market_snapshot,
                    "features": row["features"],
                    "evidence": {"source": "Quant Paper V2", "structure": stage1["structure"],
                                 "reason_codes": stage1["reason_codes"],
                                 "phase9": "NOT_CONFIGURED" if outcome is None else outcome.reason_code},
                    "strategy_state": stage1["classification"],
                    "decision": "WAIT" if stage1["category"] == "B" else ("ANALYZE" if stage1["category"] == "A" else "NO_TRADE"),
                    "confidence": None,
                    "risk_decision": (
                        "APPROVED" if outcome is not None and outcome.execution_intent is not None
                        else "NO_TRADE_BY_STRATEGY" if strategy_no_trade
                        else "REJECTED_BY_READINESS_GATE"
                    ),
                    "risk_reason": [] if outcome is not None and outcome.execution_intent is not None
                        else ([outcome.reason_code] if outcome is not None else list(gate.blockers)),
                    "execution_intent": _clean(outcome.execution_intent) if outcome is not None else None,
                    "execution_results": _clean(outcome.execution_results) if outcome is not None else [],
                    "paper_submitted": bool(outcome is not None and any(
                        event.get("reason_code") == "PAPER_SUBMIT_ACCEPTED" for event in outcome.events
                    )),
                    "position_snapshot": _clean(outcome.position_snapshot) if outcome is not None else None,
                    "reconciliation": outcome.reconciliation if outcome is not None else "NOT_READY",
                    "no_trade_classification": no_trade_classification,
                    "trade_semantics": (
                        "PAPER_EXECUTION_RECORDED" if execution_recorded else no_trade_classification
                    ),
                    "pipeline_disposition": outcome.disposition if outcome is not None else "NO_TRADE_BY_SYSTEM_NOT_READY",
                    "pipeline_reason_code": outcome.reason_code if outcome is not None else "RUNTIME_PIPELINE_NOT_CONFIGURED",
                    "pipeline_events": _clean(outcome.events) if outcome is not None else [],
                    "final_action": gate.final_action,
                }
                row.update({
                    key: payload[key] for key in (
                        "trade_semantics", "no_trade_classification", "pipeline_disposition", "pipeline_reason_code",
                        "pipeline_events", "execution_intent", "execution_results",
                        "position_snapshot", "reconciliation",
                    )
                })
                digest_payload = {"symbol": row["symbol"], "market_snapshot": market_snapshot, "stage1": stage1}
                digest = hashlib.sha256(json.dumps(_clean(digest_payload), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                self.store.record_decision(self._session_id, row["symbol"], digest, payload,
                                           data_source=row.get("_source_class", source_class), now=at)
            latest_market_event = max(
                (feed.get("event_time") for row in rows for feed in row["feeds"] if feed.get("event_time")),
                default=None,
            )
            counts = self.store.counts(self._session_id)
            snapshot = {
                "mode": "REALTIME PAPER",
                "data_source": source_class,
                "live": "DISABLED" if live_disabled else "BLOCKED_CONFIGURATION",
                "health": {
                    "process_alive": {"status": "ALIVE", "observed_at": at},
                    "collector_heartbeat": {
                        "status": "AVAILABLE" if collector_ready else "STALE",
                        "reason": collector_reason,
                        "checked_at": collector_health.get("checked_at"),
                    },
                    "fresh_market_data": {
                        "status": fresh_market_data_status,
                        "ticker_hard_stale_seconds": self.config.ticker_max_age_seconds,
                    },
                },
                "freshness_policy": [_clean(asdict(policy)) for policy in FRESHNESS_POLICY.values()],
                "data_freshness": supplemental_freshness,
                "execution_quote_freshness": {
                    "data_type": "PRICE_EXECUTION", "status": "NOT_WIRED",
                    "source_event_age": None, "ingest_lag": None, "processing_lag": None,
                    "soft_threshold_seconds": FRESHNESS_POLICY["PRICE_EXECUTION"].soft_seconds,
                    "hard_threshold_seconds": FRESHNESS_POLICY["PRICE_EXECUTION"].hard_seconds,
                    "reason": "No execution quote caller is wired; RiskPolicy quote age is capped at 5s.",
                },
                "uptime_seconds": round(time.monotonic() - self._started_monotonic, 3),
                "observed_at": at,
                "last_market_event": latest_market_event,
                "last_decision": at if rows else None,
                "last_trade": None,
                "reconciliation": "RECONCILED" if (
                    (operational_checks or checks).get("reconciliation") is True) else "NOT_READY",
                "readiness": gate.status,
                "readiness_detail": readiness_detail,
                "no_trade_classification": readiness_detail["no_trade_classification"],
                "final_action": gate.final_action,
                "blockers": list(gate.blockers),
                "checks": [{"component": name, "ready": value,
                            "status": "READY" if value else "BLOCKED",
                            "reason": (strategy_reason if name == "strategy" else
                                       collector_reason if name == "collector" else
                                       "NO_ENABLED_POLICY_TTL_OR_RUNTIME_LINK" if name in {"candidate_validity", "paper_engine", "reconciliation", "execution_wired"}
                                       else "FAILED_OR_STALE" if not value else "OK")}
                           for name, value in (operational_checks or checks).items()],
                "symbols": rows,
                "counts": counts,
                "session_id": self._session_id,
                "strategy_version": "QUANT_PAPER_V2",
                "git_commit": _git_commit(self.config.project_root),
                "data_source_note": "Only existing Phase1-8 public collector rows are accepted; no fixture fallback.",
                "strategy_ready_reason": strategy_reason,
            }
            self.store.set_data_source(self._session_id, source_class)
            self.store.record_snapshot(self._session_id, snapshot, now=at)
            self.store.record_cycle(
                self._session_id, data_source=source_class, readiness=gate.status,
                final_action=gate.final_action, now=at, readiness_detail=readiness_detail,
            )
            if self._database_connected is None:
                self._database_connected = True
            return snapshot
        except Exception as exc:
            if self._database_connected is not False:
                self.store.record_event(self._session_id, "DISCONNECT", "CANONICAL_DATABASE_UNAVAILABLE",
                                        {"exception_type": type(exc).__name__})
            self._database_connected = False
            delay = retry_delay(1, base_seconds=1, maximum_seconds=60)
            gate = assess_readiness({
                "market_data": False, "clock": wall_clock_ok, "database": False,
                "collector": False, "strategy": False, "candidate_validity": False,
                "risk": False, "paper_engine": False, "reconciliation": False,
                "live_disabled": _live_is_disabled(self.env),
                "execution_wired": False,
            })
            self.store.record_event(self._session_id, "ERROR", "CANONICAL_READ_FAILED",
                                    {"exception_type": type(exc).__name__, "retry_after_seconds": delay})
            readiness_detail = asdict(assess_paper_v1_readiness(
                {
                    "process_ready": True, "data_ready": False, "stage1_ready": False,
                    "phase9_ready": False, "policy_ready": False, "risk_ready": False,
                    "paper_ready": False, "reconciliation_ready": False, "execution_ready": False,
                },
                failure_stage="system", reason_code="CANONICAL_DATABASE_UNAVAILABLE",
            ))
            snapshot = {
                "mode": "REALTIME PAPER", "data_source": "UNKNOWN",
                "strategy_version": "QUANT_PAPER_V2",
                "live": "DISABLED" if _live_is_disabled(self.env) else "BLOCKED_CONFIGURATION",
                "uptime_seconds": round(time.monotonic() - self._started_monotonic, 3),
                "observed_at": at, "last_market_event": None, "last_decision": None, "last_trade": None,
                "reconciliation": "NOT_READY", "readiness": gate.status,
                "readiness_detail": readiness_detail, "final_action": gate.final_action,
                "health": {
                    "process_alive": {"status": "ALIVE", "observed_at": at},
                    "collector_heartbeat": {"status": "UNKNOWN", "reason": "CANONICAL_READ_FAILED"},
                    "fresh_market_data": {"status": "STALE", "reason": "CANONICAL_READ_FAILED"},
                },
                "freshness_policy": [_clean(asdict(policy)) for policy in FRESHNESS_POLICY.values()],
                "data_freshness": [],
                "execution_quote_freshness": {
                    "data_type": "PRICE_EXECUTION", "status": "NOT_WIRED",
                    "source_event_age": None, "ingest_lag": None, "processing_lag": None,
                    "soft_threshold_seconds": FRESHNESS_POLICY["PRICE_EXECUTION"].soft_seconds,
                    "hard_threshold_seconds": FRESHNESS_POLICY["PRICE_EXECUTION"].hard_seconds,
                },
                "blockers": list(gate.blockers), "checks": [
                    {"component": name, "ready": False, "status": "BLOCKED", "reason": "CANONICAL_READ_FAILED"}
                    for name in gate.blockers
                ],
                "symbols": [], "counts": self.store.counts(self._session_id),
                "session_id": self._session_id, "retry_after_seconds": delay,
            }
            self.store.record_snapshot(self._session_id, snapshot, now=at)
            self.store.record_cycle(
                self._session_id, data_source="UNKNOWN", readiness=gate.status,
                final_action=gate.final_action, now=at, readiness_detail=readiness_detail,
            )
            return snapshot

    def close(self) -> None:
        for resource in reversed(self._resources):
            try:
                resource.close()
            except Exception:
                pass
        self._resources.clear()

    def run(self, *, duration_seconds: float | None = None, stop_event=None) -> None:
        session = self.store.start_session({
            "symbols": list(self.config.symbols), "data_source": "UNKNOWN",
            "strategy_version": "QUANT_PAPER_V2",
            "config_hash": self.config.config_hash, "git_commit": _git_commit(self.config.project_root),
            "expected_duration_seconds": duration_seconds, "poll_seconds": self.config.poll_seconds,
        })
        self._session_id = session.session_id
        self.store.record_event(self._session_id, "STARTED", "REALTIME_PAPER_MONITOR_STARTED", {
            "data_source_requirement": "REAL_PUBLIC_DATA",
            "resumed_from_session_id": session.resumed_from_session_id,
        })
        end_at = time.monotonic() + duration_seconds if duration_seconds is not None else None
        failures = 0
        reason = "DURATION_COMPLETE"
        while True:
            snapshot = self.cycle()
            if snapshot["data_source"] == "UNKNOWN" or not snapshot.get("symbols"):
                failures += 1
                wait_for = retry_delay(failures, base_seconds=1, maximum_seconds=60)
            else:
                failures = 0
                wait_for = self.config.poll_seconds
            if stop_event is not None and stop_event.is_set():
                reason = "STOP_REQUESTED"
                break
            if end_at is not None and time.monotonic() >= end_at:
                break
            remaining = max(0.0, end_at - time.monotonic()) if end_at is not None else wait_for
            timeout = min(wait_for, remaining) if end_at is not None else wait_for
            if stop_event is not None:
                stop_event.wait(timeout)
            else:
                time.sleep(timeout)
        self.store.end_session(self._session_id, reason=reason)
