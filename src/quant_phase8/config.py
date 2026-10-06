"""Validated, bounded configuration for the opt-in Phase 8 pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Mapping


_MIB = 1024 * 1024


def _raw(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get(name, default).strip()


def _int(
    env: Mapping[str, str], name: str, default: int, *, minimum: int = 1, maximum: int | None = None
) -> int:
    raw = _raw(env, name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if isinstance(value, bool) or value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"{name} must be {bound}")
    return value


def _float(
    env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    raw = _raw(env, name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"{name} must be {bound}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _raw(env, name, "" if default is False else "true").lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be one of 0, 1, true, false, yes, or no")


@dataclass(frozen=True, slots=True)
class Phase8Settings:
    """Phase 8-only settings; contains no credentials or trading controls."""

    enabled: bool
    trading_mode: str
    expiry_days: int
    max_abs_moneyness_pct: float
    max_ticker_instruments_per_currency: int
    max_ticker_subscriptions_total: int
    max_full_chain_records_per_underlying: int
    max_full_chain_records_total: int
    max_markprice_channels_total: int
    instrument_refresh_seconds: int
    reseed_after_outage_seconds: int
    chain_snapshot_interval_seconds: int
    markprice_snapshot_interval_seconds: int
    ticker_snapshot_interval_seconds: int
    context_interval_seconds: int
    rest_max_response_bytes: int
    rest_timeout_seconds: float
    rest_min_interval_seconds: float
    rest_max_cooldown_seconds: int
    rest_max_attempts: int
    ws_queue_max_messages: int
    ws_max_queued_bytes: int
    ws_max_message_bytes: int
    ws_subscribe_min_interval_seconds: float
    ws_subscribe_batch_size: int
    ws_max_reconnect_attempts: int
    ws_reconnect_max_delay_seconds: int
    shutdown_timeout_seconds: int
    snapshot_retention_days: int
    context_retention_days: int
    lifecycle_retention_days: int
    retention_enforcement: bool
    retention_delete_batch_rows: int
    chain_stale_after_seconds: int
    ticker_stale_after_seconds: int
    markprice_stale_after_seconds: int
    catalog_stale_after_seconds: int
    lifecycle_stale_after_seconds: int
    max_source_skew_seconds: int
    min_chain_coverage: float
    min_ticker_coverage: float
    concentration_top_n: int
    skew_min_contracts: int
    skew_min_distinct_strikes: int
    rr_delta_tolerance: float | None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Phase8Settings":
        env = os.environ if environ is None else environ
        trading_mode = _raw(env, "TRADING_MODE", "paper").lower()
        if trading_mode != "paper":
            raise ValueError("Phase 8 requires TRADING_MODE=paper")

        max_ticker_per_currency = _int(
            env, "PHASE8_OPTIONS_MAX_TICKER_INSTRUMENTS_PER_CURRENCY", 64, maximum=64
        )
        max_ticker_total = _int(
            env, "PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL", 128, maximum=128
        )
        if max_ticker_total < max_ticker_per_currency:
            raise ValueError("PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL must be at least the per-currency cap")

        max_chain_per_underlying = _int(
            env, "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING", 2048, maximum=2048
        )
        max_chain_total = _int(
            env, "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL", 4096, maximum=4096
        )
        if max_chain_total < max_chain_per_underlying:
            raise ValueError("PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL must be at least the per-underlying cap")

        ws_message_bytes = _int(
            env, "PHASE8_OPTIONS_WS_MAX_MESSAGE_BYTES", _MIB, maximum=4 * _MIB
        )
        ws_queued_bytes = _int(
            env, "PHASE8_OPTIONS_WS_MAX_QUEUED_BYTES", 16 * _MIB, maximum=16 * _MIB
        )
        if ws_queued_bytes < ws_message_bytes:
            raise ValueError("PHASE8_OPTIONS_WS_MAX_QUEUED_BYTES must fit at least one maximum message")

        rr_raw = _raw(env, "PHASE8_OPTIONS_RR_DELTA_TOLERANCE", "")
        rr_tolerance = (
            _float(env, "PHASE8_OPTIONS_RR_DELTA_TOLERANCE", 0.0, minimum=0.0, maximum=1.0)
            if rr_raw else None
        )

        return cls(
            enabled=_bool(env, "PHASE8_OPTIONS_ENABLED", False),
            trading_mode=trading_mode,
            expiry_days=_int(env, "PHASE8_OPTIONS_MAX_EXPIRY_DAYS", 90, maximum=365),
            max_abs_moneyness_pct=_float(
                env, "PHASE8_OPTIONS_MAX_ABS_MONEYNESS_PCT", 5.0, maximum=100.0
            ),
            max_ticker_instruments_per_currency=max_ticker_per_currency,
            max_ticker_subscriptions_total=max_ticker_total,
            max_full_chain_records_per_underlying=max_chain_per_underlying,
            max_full_chain_records_total=max_chain_total,
            max_markprice_channels_total=_int(
                env, "PHASE8_OPTIONS_MAX_MARKPRICE_CHANNELS_TOTAL", 4, maximum=4
            ),
            instrument_refresh_seconds=_int(env, "PHASE8_OPTIONS_INSTRUMENT_REFRESH_SECONDS", 86_400),
            reseed_after_outage_seconds=_int(env, "PHASE8_OPTIONS_RESEED_AFTER_OUTAGE_SECONDS", 300),
            chain_snapshot_interval_seconds=_int(env, "PHASE8_OPTIONS_CHAIN_SNAPSHOT_INTERVAL_SECONDS", 3600),
            markprice_snapshot_interval_seconds=_int(env, "PHASE8_OPTIONS_MARKPRICE_SNAPSHOT_INTERVAL_SECONDS", 900),
            ticker_snapshot_interval_seconds=_int(env, "PHASE8_OPTIONS_TICKER_SNAPSHOT_INTERVAL_SECONDS", 900),
            context_interval_seconds=_int(env, "PHASE8_OPTIONS_CONTEXT_INTERVAL_SECONDS", 900),
            rest_max_response_bytes=_int(
                env, "PHASE8_OPTIONS_MAX_REST_RESPONSE_BYTES", 8 * _MIB, maximum=32 * _MIB
            ),
            rest_timeout_seconds=_float(env, "PHASE8_OPTIONS_REST_TIMEOUT_SECONDS", 10.0, maximum=60.0),
            rest_min_interval_seconds=_float(env, "PHASE8_OPTIONS_REST_MIN_INTERVAL_SECONDS", 1.25),
            rest_max_cooldown_seconds=_int(env, "PHASE8_OPTIONS_REST_MAX_COOLDOWN_SECONDS", 300, maximum=300),
            rest_max_attempts=_int(env, "PHASE8_OPTIONS_REST_MAX_ATTEMPTS", 3, maximum=3),
            ws_queue_max_messages=_int(env, "PHASE8_OPTIONS_WS_QUEUE_MAX_MESSAGES", 256, maximum=256),
            ws_max_queued_bytes=ws_queued_bytes,
            ws_max_message_bytes=ws_message_bytes,
            ws_subscribe_min_interval_seconds=_float(
                env, "PHASE8_OPTIONS_WS_SUBSCRIBE_MIN_INTERVAL_SECONDS", 0.35, minimum=0.35
            ),
            ws_subscribe_batch_size=_int(env, "PHASE8_OPTIONS_WS_SUBSCRIBE_BATCH_SIZE", 32, maximum=32),
            ws_max_reconnect_attempts=_int(env, "PHASE8_OPTIONS_WS_MAX_RECONNECT_ATTEMPTS", 8, maximum=8),
            ws_reconnect_max_delay_seconds=_int(
                env, "PHASE8_OPTIONS_WS_RECONNECT_MAX_DELAY_SECONDS", 60, maximum=60
            ),
            shutdown_timeout_seconds=_int(env, "PHASE8_OPTIONS_SHUTDOWN_TIMEOUT_SECONDS", 10, maximum=30),
            snapshot_retention_days=_int(env, "PHASE8_OPTIONS_SNAPSHOT_RETENTION_DAYS", 7, maximum=3650),
            context_retention_days=_int(env, "PHASE8_OPTIONS_CONTEXT_RETENTION_DAYS", 30, maximum=3650),
            lifecycle_retention_days=_int(env, "PHASE8_OPTIONS_LIFECYCLE_RETENTION_DAYS", 90, maximum=3650),
            retention_enforcement=_bool(env, "PHASE8_OPTIONS_RETENTION_ENFORCEMENT", False),
            retention_delete_batch_rows=_int(
                env, "PHASE8_OPTIONS_RETENTION_DELETE_BATCH_ROWS", 1000, maximum=1000
            ),
            chain_stale_after_seconds=_int(env, "PHASE8_OPTIONS_CHAIN_STALE_AFTER_SECONDS", 5400),
            ticker_stale_after_seconds=_int(env, "PHASE8_OPTIONS_TICKER_STALE_AFTER_SECONDS", 300),
            markprice_stale_after_seconds=_int(env, "PHASE8_OPTIONS_MARKPRICE_STALE_AFTER_SECONDS", 900),
            catalog_stale_after_seconds=_int(env, "PHASE8_OPTIONS_CATALOG_STALE_AFTER_SECONDS", 93_600),
            lifecycle_stale_after_seconds=_int(env, "PHASE8_OPTIONS_LIFECYCLE_STALE_AFTER_SECONDS", 60),
            max_source_skew_seconds=_int(env, "PHASE8_OPTIONS_MAX_SOURCE_SKEW_SECONDS", 5400),
            min_chain_coverage=_float(env, "PHASE8_OPTIONS_MIN_CHAIN_COVERAGE", 0.95, maximum=1.0),
            min_ticker_coverage=_float(env, "PHASE8_OPTIONS_MIN_TICKER_COVERAGE", 0.90, maximum=1.0),
            concentration_top_n=_int(env, "PHASE8_OPTIONS_CONCENTRATION_TOP_N", 20, maximum=100),
            skew_min_contracts=_int(env, "PHASE8_OPTIONS_SKEW_MIN_CONTRACTS", 6, maximum=128),
            skew_min_distinct_strikes=_int(env, "PHASE8_OPTIONS_SKEW_MIN_DISTINCT_STRIKES", 3, maximum=64),
            rr_delta_tolerance=rr_tolerance,
        )
