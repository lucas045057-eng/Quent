"""Validated Phase 1 configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Mapping
from urllib.parse import urlparse

from pydantic import SecretStr


INTERVALS = ("5m", "15m", "1H", "4H")
PUBLIC_WS_CLOSE_TIMEOUT_SECONDS = 2.0
PHASE7_RPC_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
PHASE7_RPC_DEFAULT_RESPONSE_BYTES = 8 * 1024 * 1024
PHASE7_BITCOIN_DEFAULT_RESPONSE_BYTES = 32 * 1024 * 1024


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _nonnegative_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative number") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def _bounded_positive_int(env: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    value = _positive_int(env, name, default)
    if value > maximum:
        raise ValueError(f"{name} cannot exceed {maximum}")
    return value


def _bounded_float(
    env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float
) -> float:
    value = _positive_float(env, name, default)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _strict_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be one of 0, 1, true, false, yes, or no")


@dataclass(frozen=True, slots=True)
class Phase7RpcSourceSettings:
    """Secret-bearing Phase 7 RPC configuration with a safe representation."""

    source_id: str
    enabled: bool
    endpoint: str = field(default="", repr=False)
    auth_mode: str = "NONE"
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    timeout_seconds: float = 8.0
    requests_per_second: float = 1.0
    max_concurrency: int = 1
    api_key: SecretStr | None = field(default=None, repr=False)
    max_response_bytes: int = PHASE7_RPC_DEFAULT_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1 <= self.max_response_bytes <= PHASE7_RPC_MAX_RESPONSE_BYTES
        ):
            raise ValueError(
                f"max_response_bytes must be between 1 and {PHASE7_RPC_MAX_RESPONSE_BYTES}"
            )

    @property
    def endpoint_host(self) -> str | None:
        if not self.endpoint:
            return None
        parsed = urlparse(self.endpoint)
        if not parsed.hostname:
            return None
        try:
            port = parsed.port
        except ValueError:
            return parsed.hostname
        return f"{parsed.hostname}:{port}" if port is not None else parsed.hostname


def _phase7_rpc_settings(
    env: Mapping[str, str],
    *,
    source_id: str,
    prefix: str,
    allow_basic_auth: bool,
    allow_api_key_header: bool = False,
    response_limit_name: str | None = None,
    default_response_bytes: int = PHASE7_RPC_DEFAULT_RESPONSE_BYTES,
) -> Phase7RpcSourceSettings:
    enabled = _strict_bool(env, f"{prefix}_ENABLED", False)
    endpoint = env.get(f"{prefix}_URL", "").strip()
    auth_mode = env.get(f"{prefix}_AUTH_MODE", "none").strip().upper() or "NONE"
    allowed_auth = {"NONE", "URL"}
    if allow_basic_auth:
        allowed_auth.add("BASIC")
    if allow_api_key_header:
        allowed_auth.add("API_KEY_HEADER")
    if auth_mode not in allowed_auth:
        allowed = ", ".join(sorted(allowed_auth))
        raise ValueError(f"{prefix}_AUTH_MODE must be one of {allowed}")

    if enabled:
        if not endpoint:
            raise ValueError(f"{prefix}_URL is required when {prefix}_ENABLED=true")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{prefix}_URL must be an absolute HTTP(S) URL")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError(f"{prefix}_URL must contain a valid port") from exc

    username = env.get(f"{prefix}_USERNAME", "")
    password = env.get(f"{prefix}_PASSWORD", "")
    api_key_value = env.get(f"{prefix}_API_KEY", "")
    if auth_mode == "BASIC" and enabled and (not username or not password):
        raise ValueError(f"{prefix}_USERNAME and {prefix}_PASSWORD are required for BASIC auth")
    if auth_mode != "BASIC" and (username or password):
        raise ValueError(f"{prefix}_USERNAME/PASSWORD require {prefix}_AUTH_MODE=BASIC")
    if auth_mode == "API_KEY_HEADER" and not api_key_value.strip():
        raise ValueError(f"{prefix}_API_KEY is required for API_KEY_HEADER auth")
    if auth_mode != "API_KEY_HEADER" and api_key_value:
        raise ValueError(f"{prefix}_API_KEY requires {prefix}_AUTH_MODE=API_KEY_HEADER")

    return Phase7RpcSourceSettings(
        source_id=source_id,
        enabled=enabled,
        endpoint=endpoint,
        auth_mode=auth_mode,
        username=username,
        password=password,
        timeout_seconds=_bounded_float(
            env, f"{prefix}_TIMEOUT_SECONDS", 8.0, 0.1, 60.0
        ),
        requests_per_second=_bounded_float(
            env, f"{prefix}_REQUESTS_PER_SECOND", 1.0, 0.1, 100.0
        ),
        max_concurrency=_bounded_positive_int(
            env, f"{prefix}_MAX_CONCURRENCY", 1, 8
        ),
        api_key=SecretStr(api_key_value) if api_key_value else None,
        max_response_bytes=(
            _bounded_positive_int(
                env, response_limit_name, default_response_bytes,
                PHASE7_RPC_MAX_RESPONSE_BYTES,
            )
            if response_limit_name is not None else default_response_bytes
        ),
    )


@dataclass(frozen=True, slots=True)
class Settings:
    trading_mode: str
    rest_base_url: str
    ws_public_url: str
    postgres_dsn: str = field(repr=False)
    kline_retention_days: dict[str, int]
    kline_ingestion_grace_seconds: dict[str, int]
    market_snapshot_retention_days: int = 7
    kline_fetch_limit: int = 100
    rest_requests_per_second: int = 20
    universe_limit: int = 200
    universe_interval_seconds: float = 3600.0
    stage1_interval_seconds: float = 300.0
    ws_reconnect_seconds: float = 5.0
    ticker_persist_interval_seconds: float = 5.0
    # How long a DEGRADED runtime state must persist before the persisted
    # heartbeat reports ERROR.  Sub-second WebSocket 1006/timeout blips heal
    # inside this window and stay AVAILABLE (still visible via runtime_state
    # in details); a genuine sustained outage must surface well inside the
    # consumer's 30s collector heartbeat ceiling, so the bound stays below it.
    health_degraded_grace_seconds: float = 15.0
    event_buffer_capacity: int = 2000
    phase2_enabled: bool = False
    bitget_oi_unit_contract_path: str | None = None
    phase2_interval_seconds: float = 300.0
    # Empty means the Phase 2 runtime must consume the persisted Phase 1
    # Top-N Universe. An explicit list is retained only for isolated tests or
    # a deliberately scoped operational override.
    phase2_symbols: tuple[str, ...] = ()
    phase2_min_oi_sources: int = 2
    phase2_min_funding_sources: int = 2
    phase2_oi_freshness_seconds: int = 600
    phase2_funding_freshness_seconds: int = 600
    phase2_oi_retention_days: int = 30
    phase2_funding_retention_days: int = 90
    phase2_snapshot_retention_days: int = 30
    # Phase 3 remains opt-in until runtime acceptance. These limits are
    # intentionally bounded for the existing two-container deployment.
    phase3_enabled: bool = False
    max_trade_stream_symbols: int = 20
    trade_min_subscription_seconds: int = 300
    trade_subscription_cooldown_seconds: int = 60
    max_trade_queue_size: int = 2000
    trade_dedup_max_entries_per_exchange: int = 10_000
    trade_dedup_ttl_seconds: int = 300
    trade_allowed_lateness_seconds: int = 5
    phase3_flow_retention_days: dict[str, int] = field(
        default_factory=lambda: {"1m": 7, "5m": 30, "15m": 90, "1H": 180, "4H": 365}
    )
    phase3_cvd_retention_days: int = 365
    phase3_gap_retention_days: int = 90
    phase3_cross_exchange_retention_days: int = 180
    phase3_enrichment_retention_days: int = 90
    phase3_min_directional_sources: int = 2
    # Phase 4 is opt-in and limited to official public market-data sources.
    phase4_enabled: bool = False
    phase4_liquidation_event_retention_hours: int = 24
    phase4_liquidation_retention_days: dict[str, int] = field(
        default_factory=lambda: {"1m": 7, "5m": 30, "15m": 90, "1H": 180, "4H": 365}
    )
    phase4_long_short_retention_days: int = 90
    phase4_basis_retention_days: int = 90
    phase4_cross_exchange_retention_days: int = 90
    phase4_enrichment_retention_days: int = 90
    phase4_max_basis_timestamp_skew_seconds: int = 5
    phase4_queue_capacity: int = 2_000
    # The per-key queue is not multiplied by the symbol universe: these two
    # global limits keep both exchanges admissible within the 256 MiB collector.
    phase4_event_budget: int = 20_000
    phase4_event_bytes_budget: int = 32 * 1024 * 1024
    phase4_builder_window_budget: int = 20_000
    phase4_finalized_window_budget: int = 40_000
    phase4_rollup_window_budget: int = 100_000
    phase4_hydration_windows_per_key: int = 240
    phase4_rest_cycle_seconds: float = 60.0
    phase4_bitget_classic_rest_base_url: str = "https://api.bitget.com"
    phase4_bitget_uta_rest_base_url: str = "https://api.bitget.com"
    phase4_bitget_uta_ws_public_url: str = "wss://ws.bitget.com/v3/ws/public"
    phase4_bybit_rest_base_url: str = "https://api.bybit.com"
    phase4_bybit_ws_public_linear_url: str = "wss://stream.bybit.com/v5/public/linear"
    phase4_hyperliquid_info_url: str = "https://api.hyperliquid.xyz/info"
    # Phase 5 is opt-in, deterministic, and context-only. It never changes
    # Stage1 eligibility or creates a trading action.
    phase5_enabled: bool = False
    phase5_min_closed_bars: int = 21
    phase5_return_lookback_bars: int = 3
    phase5_vol_window_bars: int = 20
    phase5_volume_baseline_bars: int = 20
    phase5_min_universe_sample: int = 20
    phase5_min_breadth_coverage: float = 0.80
    phase5_breadth_neutral_band_pct: float = 0.05
    phase5_breadth_broad_ratio: float = 0.60
    phase5_breadth_narrow_ratio: float = 0.55
    phase5_min_sector_members: int = 5
    phase5_min_market_benchmark_sample: int = 20
    phase5_max_context_timestamp_skew_seconds: int = 60
    phase5_max_evidence_bytes: int = 65_536
    phase5_ema_fast_period: int = 9
    phase5_ema_slow_period: int = 21
    phase5_slope_threshold_pct: float = 0.10
    phase5_volume_below_ratio: float = 0.80
    phase5_volume_elevated_ratio: float = 1.50
    phase5_volatility_thresholds: dict[str, tuple[float, float, float]] = field(
        default_factory=lambda: {
            "5m": (0.0010, 0.0040, 0.0080),
            "15m": (0.0015, 0.0060, 0.0120),
            "1H": (0.0030, 0.0120, 0.0250),
            "4H": (0.0060, 0.0250, 0.0500),
        }
    )
    phase5_context_retention_days: dict[str, int] = field(
        default_factory=lambda: {"5m": 30, "15m": 90, "1H": 180, "4H": 365}
    )
    phase5_enrichment_retention_days: int = 30
    phase5_relative_strength_thresholds_pct: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {"15m": (-0.20, 0.20), "1H": (-0.50, 0.50), "4H": (-1.00, 1.00)}
    )
    phase5_sector_thresholds_pct: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {"15m": (-0.20, 0.20), "1H": (-0.50, 0.50), "4H": (-1.00, 1.00)}
    )
    phase5_taxonomy_path: str = "config/phase5_sector_map.csv"
    # Phase 6 is opt-in, context-only, and provider-key agnostic.  Empty
    # provider names intentionally keep the runtime in deterministic degraded
    # mode until a separately approved adapter is configured.
    phase6_enabled: bool = False
    phase6_ingestion_interval_seconds: int = 300
    phase6_news_freshness_hours: int = 24
    phase6_macro_freshness_days: int = 7
    phase6_unlock_freshness_days: int = 30
    phase6_news_retention_days: int = 30
    phase6_macro_retention_days: int = 730
    phase6_unlock_retention_days: int = 730
    phase6_ai_analysis_retention_days: int = 30
    phase6_ai_usage_retention_days: int = 365
    phase6_raw_cache_ttl_hours: int = 24
    phase6_max_raw_bytes: int = 262_144
    phase6_max_ai_input_bytes: int = 65_536
    phase6_max_ai_output_bytes: int = 32_768
    phase6_ai_queue_capacity: int = 32
    phase6_ai_concurrency: int = 2
    phase6_ai_timeout_seconds: float = 10.0
    phase6_ai_max_retries: int = 1
    phase6_ai_retry_interval_seconds: int = 300
    phase6_ai_rate_interval_ms: int = 50
    phase6_ai_daily_soft_budget: float = 10.0
    phase6_ai_daily_hard_budget: float = 20.0
    phase6_ai_monthly_soft_budget: float = 100.0
    phase6_ai_monthly_hard_budget: float = 200.0
    phase6_ai_primary_provider: str = ""
    phase6_ai_fallback_provider: str = ""
    phase6_source_registry_path: str = ""
    phase7_enabled: bool = False
    # Independently enable verified public SPOT/PERP flow, without RPC services.
    bitget_sbe_flow_enabled: bool = False
    bitget_sbe_flow_scope_path: str | None = None
    phase2_symbol_concurrency: int = 1
    phase7_context_interval_seconds: int = 60
    phase7_context_lookback_hours: int = 4
    phase7_context_window_batch: int = 16
    phase7_address_labels_path: str = ""
    phase7_whale_thresholds_path: str = ""
    phase7_onchain_transfer_events_retention_days: int = 90
    phase7_address_labels_retention_days: int = 730
    phase7_onchain_flow_windows_retention_days: int = 365
    phase7_whale_flow_windows_retention_days: int = 365
    phase7_spot_flow_windows_retention_days: int = 30
    phase7_stablecoin_context_retention_days: int = 365
    phase7_ingestion_checkpoints_retention_days: int = 365
    phase7_enrichment_retention_days: int = 365
    # Phase 7 RPC endpoints may contain provider credentials in their URL.
    # Keep these fields out of Settings.__repr__ and diagnostics by design.
    phase7_bitcoin_rpc: Phase7RpcSourceSettings = field(
        default_factory=lambda: Phase7RpcSourceSettings("bitcoin_rpc", False), repr=False
    )
    phase7_ethereum_rpc: Phase7RpcSourceSettings = field(
        default_factory=lambda: Phase7RpcSourceSettings("ethereum_rpc", False), repr=False
    )

    def validate_startup(self) -> None:
        """Validate opt-in Phase 7 source configuration before runtime starts."""

        from quant_phase7.source_config import validate_phase7_source_startup

        validate_phase7_source_startup(self)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        trading_mode = env.get("TRADING_MODE", "paper").strip().lower()
        if trading_mode != "paper":
            raise ValueError("Phase 1 requires TRADING_MODE=paper")

        private_names = ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE")
        if any(env.get(name, "").strip() for name in private_names):
            raise ValueError("Phase 1 rejects private API credentials")

        retention = {
            interval: _positive_int(env, f"KLINE_RETENTION_{interval}_DAYS", default)
            for interval, default in (("5M", 30), ("15M", 90), ("1H", 180), ("4H", 365))
        }
        grace = {
            interval: _positive_int(env, f"KLINE_INGESTION_GRACE_{interval}_SECONDS", default)
            for interval, default in (("5M", 30), ("15M", 60), ("1H", 120), ("4H", 180))
        }
        kline_fetch_limit = _positive_int(env, "KLINE_FETCH_LIMIT", 100)
        if kline_fetch_limit > 1000:
            raise ValueError("KLINE_FETCH_LIMIT cannot exceed 1000")
        phase3_retention = {
            interval: _positive_int(env, f"PHASE3_FLOW_RETENTION_{env_name}_DAYS", default)
            for interval, env_name, default in (
                ("1m", "1M", 7),
                ("5m", "5M", 30),
                ("15m", "15M", 90),
                ("1H", "1H", 180),
                ("4H", "4H", 365),
            )
        }
        phase4_liquidation_retention = {
            interval: _bounded_positive_int(
                env, f"PHASE4_LIQUIDATION_{env_name}_RETENTION_DAYS", default, 365
            )
            for interval, env_name, default in (
                ("1m", "1M", 7),
                ("5m", "5M", 30),
                ("15m", "15M", 90),
                ("1H", "1H", 180),
                ("4H", "4H", 365),
            )
        }
        phase5_retention = {
            interval: _bounded_positive_int(
                env, f"PHASE5_CONTEXT_RETENTION_{env_name}_DAYS", default, 365
            )
            for interval, env_name, default in (
                ("5m", "5M", 30),
                ("15m", "15M", 90),
                ("1H", "1H", 180),
                ("4H", "4H", 365),
            )
        }
        phase6_daily_soft_budget = _nonnegative_float(env, "PHASE6_AI_DAILY_SOFT_BUDGET", 10.0)
        phase6_daily_hard_budget = _nonnegative_float(env, "PHASE6_AI_DAILY_HARD_BUDGET", 20.0)
        phase6_monthly_soft_budget = _nonnegative_float(env, "PHASE6_AI_MONTHLY_SOFT_BUDGET", 100.0)
        phase6_monthly_hard_budget = _nonnegative_float(env, "PHASE6_AI_MONTHLY_HARD_BUDGET", 200.0)
        if phase6_daily_soft_budget > phase6_daily_hard_budget:
            raise ValueError("PHASE6_AI_DAILY_SOFT_BUDGET must not exceed hard budget")
        if phase6_monthly_soft_budget > phase6_monthly_hard_budget:
            raise ValueError("PHASE6_AI_MONTHLY_SOFT_BUDGET must not exceed hard budget")
        phase5_breadth_coverage = _bounded_float(
            env, "PHASE5_MIN_BREADTH_COVERAGE", 0.80, 0.000001, 1.0
        )
        phase5_breadth_broad_ratio = _bounded_float(
            env, "PHASE5_BREADTH_BROAD_RATIO", 0.60, 0.000001, 1.0
        )
        phase5_breadth_narrow_ratio = _bounded_float(
            env, "PHASE5_BREADTH_NARROW_RATIO", 0.55, 0.000001, 1.0
        )
        if phase5_breadth_narrow_ratio >= phase5_breadth_broad_ratio:
            raise ValueError("PHASE5_BREADTH_NARROW_RATIO must be below PHASE5_BREADTH_BROAD_RATIO")
        phase5_volatility_thresholds: dict[str, tuple[float, float, float]] = {}
        for interval, env_name, defaults in (
            ("5m", "5M", (0.0010, 0.0040, 0.0080)),
            ("15m", "15M", (0.0015, 0.0060, 0.0120)),
            ("1H", "1H", (0.0030, 0.0120, 0.0250)),
            ("4H", "4H", (0.0060, 0.0250, 0.0500)),
        ):
            low = _bounded_float(env, f"PHASE5_VOL_LOW_{env_name}", defaults[0], 0.000001, 100.0)
            high = _bounded_float(env, f"PHASE5_VOL_HIGH_{env_name}", defaults[1], 0.000001, 100.0)
            extreme = _bounded_float(
                env, f"PHASE5_VOL_EXTREME_{env_name}", defaults[2], 0.000001, 100.0
            )
            if not low < high < extreme:
                raise ValueError(f"Phase 5 volatility thresholds must increase for {interval}")
            phase5_volatility_thresholds[interval] = (low, high, extreme)
        phase5_relative_strength_thresholds: dict[str, tuple[float, float]] = {}
        phase5_sector_thresholds: dict[str, tuple[float, float]] = {}
        for interval, env_name, default in (
            ("15m", "15M", 0.20),
            ("1H", "1H", 0.50),
            ("4H", "4H", 1.00),
        ):
            rs_negative = _bounded_float(
                env, f"PHASE5_RS_NEGATIVE_{env_name}_PCT", default, 0.000001, 100.0
            )
            rs_positive = _bounded_float(
                env, f"PHASE5_RS_POSITIVE_{env_name}_PCT", default, 0.000001, 100.0
            )
            sector_negative = _bounded_float(
                env, f"PHASE5_SECTOR_NEGATIVE_{env_name}_PCT", default, 0.000001, 100.0
            )
            sector_positive = _bounded_float(
                env, f"PHASE5_SECTOR_POSITIVE_{env_name}_PCT", default, 0.000001, 100.0
            )
            phase5_relative_strength_thresholds[interval] = (-rs_negative, rs_positive)
            phase5_sector_thresholds[interval] = (-sector_negative, sector_positive)
        phase5_ema_fast_period = _bounded_positive_int(env, "PHASE5_EMA_FAST_PERIOD", 9, 1_000)
        phase5_ema_slow_period = _bounded_positive_int(env, "PHASE5_EMA_SLOW_PERIOD", 21, 2_000)
        if phase5_ema_fast_period >= phase5_ema_slow_period:
            raise ValueError("PHASE5_EMA_FAST_PERIOD must be below PHASE5_EMA_SLOW_PERIOD")
        phase5_volume_below_ratio = _bounded_float(
            env, "PHASE5_VOLUME_BELOW_RATIO", 0.80, 0.000001, 100.0
        )
        phase5_volume_elevated_ratio = _bounded_float(
            env, "PHASE5_VOLUME_ELEVATED_RATIO", 1.50, 0.000001, 100.0
        )
        if phase5_volume_below_ratio >= phase5_volume_elevated_ratio:
            raise ValueError("PHASE5_VOLUME_BELOW_RATIO must be below PHASE5_VOLUME_ELEVATED_RATIO")
        phase7_bitcoin_rpc = _phase7_rpc_settings(
            env,
            source_id="bitcoin_rpc",
            prefix="PHASE7_BITCOIN_RPC",
            allow_basic_auth=True,
            allow_api_key_header=True,
            response_limit_name="PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES",
            default_response_bytes=PHASE7_BITCOIN_DEFAULT_RESPONSE_BYTES,
        )
        phase7_ethereum_rpc = _phase7_rpc_settings(
            env,
            source_id="ethereum_rpc",
            prefix="PHASE7_ETHEREUM_RPC",
            allow_basic_auth=False,
        )
        settings = cls(
            trading_mode=trading_mode,
            rest_base_url=env.get("BITGET_REST_BASE_URL", "https://api.bitget.com").rstrip("/"),
            ws_public_url=env.get("BITGET_WS_PUBLIC_URL", "wss://ws.bitget.com/v3/ws/public"),
            postgres_dsn=env.get("POSTGRES_DSN", "postgresql://quant:quant@postgres:5432/quant"),
            kline_retention_days={interval.lower() if interval.endswith("M") else interval: value for interval, value in retention.items()},
            market_snapshot_retention_days=_positive_int(env, "MARKET_SNAPSHOT_RETENTION_DAYS", 7),
            kline_ingestion_grace_seconds={interval.lower() if interval.endswith("M") else interval: value for interval, value in grace.items()},
            kline_fetch_limit=kline_fetch_limit,
            universe_limit=_bounded_positive_int(env, "UNIVERSE_LIMIT", 200, 1000),
            universe_interval_seconds=_positive_float(env, "UNIVERSE_INTERVAL_SECONDS", 3600.0),
            stage1_interval_seconds=_positive_float(env, "STAGE1_INTERVAL_SECONDS", 300.0),
            ws_reconnect_seconds=_positive_float(env, "WS_RECONNECT_SECONDS", 5.0),
            ticker_persist_interval_seconds=_positive_float(env, "TICKER_PERSIST_INTERVAL_SECONDS", 5.0),
            health_degraded_grace_seconds=_bounded_float(env, "HEALTH_DEGRADED_GRACE_SECONDS", 15.0, 0.1, 29.9),
            event_buffer_capacity=_positive_int(env, "EVENT_BUFFER_CAPACITY", 2000),
            phase2_enabled=env.get("PHASE2_ENABLED", "0").strip().lower() in {"1", "true", "yes"},
            bitget_oi_unit_contract_path=env.get('BITGET_OI_UNIT_CONTRACT_PATH') or None,
            phase2_interval_seconds=_positive_float(env, "PHASE2_INTERVAL_SECONDS", 300.0),
            phase2_symbols=tuple(symbol.strip().upper() for symbol in env.get("PHASE2_SYMBOLS", "").split(",") if symbol.strip()),
            phase2_min_oi_sources=_positive_int(env, "PHASE2_MIN_OI_SOURCES", 2),
            phase2_min_funding_sources=_positive_int(env, "PHASE2_MIN_FUNDING_SOURCES", 2),
            phase2_oi_freshness_seconds=_positive_int(env, "PHASE2_OI_FRESHNESS_SECONDS", 600),
            phase2_funding_freshness_seconds=_positive_int(env, "PHASE2_FUNDING_FRESHNESS_SECONDS", 600),
            phase2_oi_retention_days=_positive_int(env, "PHASE2_OI_RETENTION_DAYS", 30),
            phase2_funding_retention_days=_positive_int(env, "PHASE2_FUNDING_RETENTION_DAYS", 90),
            phase2_snapshot_retention_days=_positive_int(env, "PHASE2_SNAPSHOT_RETENTION_DAYS", 30),
            phase3_enabled=env.get("PHASE3_ENABLED", "0").strip().lower() in {"1", "true", "yes"},
            max_trade_stream_symbols=_positive_int(env, "MAX_TRADE_STREAM_SYMBOLS", 20),
            trade_min_subscription_seconds=_positive_int(env, "TRADE_MIN_SUBSCRIPTION_SECONDS", 300),
            trade_subscription_cooldown_seconds=_positive_int(env, "TRADE_SUBSCRIPTION_COOLDOWN_SECONDS", 60),
            max_trade_queue_size=_positive_int(env, "MAX_TRADE_QUEUE_SIZE", 2000),
            trade_dedup_max_entries_per_exchange=_positive_int(
                env, "TRADE_DEDUP_MAX_ENTRIES_PER_EXCHANGE", 10_000
            ),
            trade_dedup_ttl_seconds=_positive_int(env, "TRADE_DEDUP_TTL_SECONDS", 300),
            trade_allowed_lateness_seconds=_positive_int(env, "TRADE_ALLOWED_LATENESS_SECONDS", 5),
            phase3_flow_retention_days=phase3_retention,
            phase3_cvd_retention_days=_positive_int(env, "PHASE3_CVD_RETENTION_DAYS", 365),
            phase3_gap_retention_days=_positive_int(env, "PHASE3_GAP_RETENTION_DAYS", 90),
            phase3_cross_exchange_retention_days=_positive_int(
                env, "PHASE3_CROSS_EXCHANGE_RETENTION_DAYS", 180
            ),
            phase3_enrichment_retention_days=_positive_int(
                env, "PHASE3_ENRICHMENT_RETENTION_DAYS", 90
            ),
            phase3_min_directional_sources=_positive_int(env, "PHASE3_MIN_DIRECTIONAL_SOURCES", 2),
            phase4_enabled=_strict_bool(env, "PHASE4_ENABLED", False),
            phase4_liquidation_event_retention_hours=_bounded_positive_int(
                env, "PHASE4_LIQUIDATION_EVENT_RETENTION_HOURS", 24, 168
            ),
            phase4_liquidation_retention_days=phase4_liquidation_retention,
            phase4_long_short_retention_days=_bounded_positive_int(
                env, "PHASE4_LONG_SHORT_RETENTION_DAYS", 90, 365
            ),
            phase4_basis_retention_days=_bounded_positive_int(
                env, "PHASE4_BASIS_RETENTION_DAYS", 90, 365
            ),
            phase4_cross_exchange_retention_days=_bounded_positive_int(
                env, "PHASE4_CROSS_EXCHANGE_RETENTION_DAYS", 90, 365
            ),
            phase4_enrichment_retention_days=_bounded_positive_int(
                env, "PHASE4_ENRICHMENT_RETENTION_DAYS", 90, 365
            ),
            phase4_max_basis_timestamp_skew_seconds=_bounded_positive_int(
                env, "PHASE4_MAX_BASIS_TIMESTAMP_SKEW_SECONDS", 5, 3_600
            ),
            phase4_queue_capacity=_bounded_positive_int(env, "PHASE4_QUEUE_CAPACITY", 2_000, 10_000),
            phase4_event_budget=_bounded_positive_int(env, "PHASE4_EVENT_BUDGET", 20_000, 100_000),
            phase4_event_bytes_budget=_bounded_positive_int(
                env, "PHASE4_EVENT_BYTES_BUDGET", 32 * 1024 * 1024, 64 * 1024 * 1024
            ),
            phase4_builder_window_budget=_bounded_positive_int(
                env, "PHASE4_BUILDER_WINDOW_BUDGET", 20_000, 100_000
            ),
            phase4_finalized_window_budget=_bounded_positive_int(
                env, "PHASE4_FINALIZED_WINDOW_BUDGET", 40_000, 200_000
            ),
            phase4_rollup_window_budget=_bounded_positive_int(
                env, "PHASE4_ROLLUP_WINDOW_BUDGET", 100_000, 400_000
            ),
            phase4_hydration_windows_per_key=_bounded_positive_int(
                env, "PHASE4_HYDRATION_WINDOWS_PER_KEY", 240, 1_440
            ),
            phase4_rest_cycle_seconds=_bounded_float(
                env, "PHASE4_REST_CYCLE_SECONDS", 60.0, 1.0, 3_600.0
            ),
            phase4_bitget_classic_rest_base_url=env.get(
                "PHASE4_BITGET_CLASSIC_REST_BASE_URL", "https://api.bitget.com"
            ).rstrip("/"),
            phase4_bitget_uta_rest_base_url=env.get(
                "PHASE4_BITGET_UTA_REST_BASE_URL", "https://api.bitget.com"
            ).rstrip("/"),
            phase4_bitget_uta_ws_public_url=env.get(
                "PHASE4_BITGET_UTA_WS_PUBLIC_URL", "wss://ws.bitget.com/v3/ws/public"
            ),
            phase4_bybit_rest_base_url=env.get(
                "PHASE4_BYBIT_REST_BASE_URL", "https://api.bybit.com"
            ).rstrip("/"),
            phase4_bybit_ws_public_linear_url=env.get(
                "PHASE4_BYBIT_WS_PUBLIC_LINEAR_URL", "wss://stream.bybit.com/v5/public/linear"
            ),
            phase4_hyperliquid_info_url=env.get(
                "PHASE4_HYPERLIQUID_INFO_URL", "https://api.hyperliquid.xyz/info"
            ).rstrip("/"),
            phase5_enabled=_strict_bool(env, "PHASE5_ENABLED", False),
            phase5_min_closed_bars=_bounded_positive_int(env, "PHASE5_MIN_CLOSED_BARS", 21, 10_000),
            phase5_return_lookback_bars=_bounded_positive_int(
                env, "PHASE5_RETURN_LOOKBACK_BARS", 3, 10_000
            ),
            phase5_vol_window_bars=_bounded_positive_int(env, "PHASE5_VOL_WINDOW_BARS", 20, 10_000),
            phase5_volume_baseline_bars=_bounded_positive_int(
                env, "PHASE5_VOLUME_BASELINE_BARS", 20, 10_000
            ),
            phase5_min_universe_sample=_bounded_positive_int(
                env, "PHASE5_MIN_UNIVERSE_SAMPLE", 20, 100_000
            ),
            phase5_min_breadth_coverage=phase5_breadth_coverage,
            phase5_breadth_neutral_band_pct=_bounded_float(
                env, "PHASE5_BREADTH_NEUTRAL_BAND_PCT", 0.05, 0.000001, 100.0
            ),
            phase5_breadth_broad_ratio=phase5_breadth_broad_ratio,
            phase5_breadth_narrow_ratio=phase5_breadth_narrow_ratio,
            phase5_min_sector_members=_bounded_positive_int(
                env, "PHASE5_MIN_SECTOR_MEMBERS", 5, 100_000
            ),
            phase5_min_market_benchmark_sample=_bounded_positive_int(
                env, "PHASE5_MIN_MARKET_BENCHMARK_SAMPLE", 20, 100_000
            ),
            phase5_max_context_timestamp_skew_seconds=_bounded_positive_int(
                env, "PHASE5_MAX_CONTEXT_TIMESTAMP_SKEW_SECONDS", 60, 86_400
            ),
            phase5_max_evidence_bytes=_bounded_positive_int(
                env, "PHASE5_MAX_EVIDENCE_BYTES", 65_536, 1_048_576
            ),
            phase5_ema_fast_period=phase5_ema_fast_period,
            phase5_ema_slow_period=phase5_ema_slow_period,
            phase5_slope_threshold_pct=_bounded_float(
                env, "PHASE5_SLOPE_THRESHOLD_PCT", 0.10, 0.000001, 100.0
            ),
            phase5_volume_below_ratio=phase5_volume_below_ratio,
            phase5_volume_elevated_ratio=phase5_volume_elevated_ratio,
            phase5_volatility_thresholds=phase5_volatility_thresholds,
            phase5_context_retention_days=phase5_retention,
            phase5_enrichment_retention_days=_bounded_positive_int(
                env, "PHASE5_ENRICHMENT_RETENTION_DAYS", 30, 365
            ),
            phase5_relative_strength_thresholds_pct=phase5_relative_strength_thresholds,
            phase5_sector_thresholds_pct=phase5_sector_thresholds,
            phase5_taxonomy_path=env.get(
                "PHASE5_TAXONOMY_PATH", "config/phase5_sector_map.csv"
            ).strip(),
            phase6_enabled=_strict_bool(env, "PHASE6_ENABLED", False),
            phase6_ingestion_interval_seconds=_bounded_positive_int(
                env, "PHASE6_INGESTION_INTERVAL_SECONDS", 300, 86_400
            ),
            phase6_news_freshness_hours=_bounded_positive_int(
                env, "PHASE6_NEWS_FRESHNESS_HOURS", 24, 24 * 365
            ),
            phase6_macro_freshness_days=_bounded_positive_int(
                env, "PHASE6_MACRO_FRESHNESS_DAYS", 7, 3650
            ),
            phase6_unlock_freshness_days=_bounded_positive_int(
                env, "PHASE6_UNLOCK_FRESHNESS_DAYS", 30, 3650
            ),
            phase6_news_retention_days=_bounded_positive_int(
                env, "PHASE6_NEWS_RETENTION_DAYS", 30, 3650
            ),
            phase6_macro_retention_days=_bounded_positive_int(
                env, "PHASE6_MACRO_RETENTION_DAYS", 730, 3650
            ),
            phase6_unlock_retention_days=_bounded_positive_int(
                env, "PHASE6_UNLOCK_RETENTION_DAYS", 730, 3650
            ),
            phase6_ai_analysis_retention_days=_bounded_positive_int(
                env, "PHASE6_AI_ANALYSIS_RETENTION_DAYS", 30, 3650
            ),
            phase6_ai_usage_retention_days=_bounded_positive_int(
                env, "PHASE6_AI_USAGE_RETENTION_DAYS", 365, 3650
            ),
            phase6_raw_cache_ttl_hours=_bounded_positive_int(
                env, "PHASE6_RAW_CACHE_TTL_HOURS", 24, 24 * 365
            ),
            phase6_max_raw_bytes=_bounded_positive_int(
                env, "PHASE6_MAX_RAW_BYTES", 262_144, 8 * 1024 * 1024
            ),
            phase6_max_ai_input_bytes=_bounded_positive_int(
                env, "PHASE6_MAX_AI_INPUT_BYTES", 65_536, 1_048_576
            ),
            phase6_max_ai_output_bytes=_bounded_positive_int(
                env, "PHASE6_MAX_AI_OUTPUT_BYTES", 32_768, 1_048_576
            ),
            phase6_ai_queue_capacity=_bounded_positive_int(
                env, "PHASE6_AI_QUEUE_CAPACITY", 32, 10_000
            ),
            phase6_ai_concurrency=_bounded_positive_int(
                env, "PHASE6_AI_CONCURRENCY", 2, 32
            ),
            phase6_ai_timeout_seconds=_bounded_float(
                env, "PHASE6_AI_TIMEOUT_SECONDS", 10.0, 0.1, 60.0
            ),
            phase6_ai_max_retries=_bounded_positive_int(
                env, "PHASE6_AI_MAX_RETRIES", 1, 3
            ),
            phase6_ai_retry_interval_seconds=_bounded_positive_int(
                env, "PHASE6_AI_RETRY_INTERVAL_SECONDS", 300, 86_400
            ),
            phase6_ai_rate_interval_ms=_bounded_positive_int(
                env, "PHASE6_AI_RATE_INTERVAL_MS", 50, 60_000
            ),
            phase6_ai_daily_soft_budget=phase6_daily_soft_budget,
            phase6_ai_daily_hard_budget=phase6_daily_hard_budget,
            phase6_ai_monthly_soft_budget=phase6_monthly_soft_budget,
            phase6_ai_monthly_hard_budget=phase6_monthly_hard_budget,
            phase6_ai_primary_provider=env.get("PHASE6_AI_PRIMARY_PROVIDER", "").strip(),
            phase6_ai_fallback_provider=env.get("PHASE6_AI_FALLBACK_PROVIDER", "").strip(),
            phase6_source_registry_path=env.get("PHASE6_SOURCE_REGISTRY_PATH", "").strip(),
            phase7_bitcoin_rpc=phase7_bitcoin_rpc,
            phase7_ethereum_rpc=phase7_ethereum_rpc,
            phase7_enabled=_strict_bool(env, "PHASE7_ENABLED", False),
            bitget_sbe_flow_enabled=_strict_bool(env, "BITGET_SBE_FLOW_ENABLED", False),
            bitget_sbe_flow_scope_path=env.get("BITGET_SBE_FLOW_SCOPE_PATH") or None,
            phase2_symbol_concurrency=_bounded_positive_int(env, "PHASE2_SYMBOL_CONCURRENCY", 1, 16),
            phase7_context_interval_seconds=_bounded_positive_int(
                env, "PHASE7_CONTEXT_INTERVAL_SECONDS", 60, 3600
            ),
            phase7_context_lookback_hours=_bounded_positive_int(
                env, "PHASE7_CONTEXT_LOOKBACK_HOURS", 4, 24
            ),
            phase7_context_window_batch=_bounded_positive_int(
                env, "PHASE7_CONTEXT_WINDOW_BATCH", 16, 256
            ),
            phase7_address_labels_path=env.get("PHASE7_ADDRESS_LABELS_PATH", "").strip(),
            phase7_whale_thresholds_path=env.get("PHASE7_WHALE_THRESHOLDS_PATH", "").strip(),
            phase7_onchain_transfer_events_retention_days=_bounded_positive_int(
                env, "PHASE7_ONCHAIN_TRANSFER_EVENTS_RETENTION_DAYS", 90, 3650
            ),
            phase7_address_labels_retention_days=_bounded_positive_int(
                env, "PHASE7_ADDRESS_LABELS_RETENTION_DAYS", 730, 3650
            ),
            phase7_onchain_flow_windows_retention_days=_bounded_positive_int(
                env, "PHASE7_ONCHAIN_FLOW_WINDOWS_RETENTION_DAYS", 365, 3650
            ),
            phase7_whale_flow_windows_retention_days=_bounded_positive_int(
                env, "PHASE7_WHALE_FLOW_WINDOWS_RETENTION_DAYS", 365, 3650
            ),
            phase7_spot_flow_windows_retention_days=_bounded_positive_int(
                env, "PHASE7_SPOT_FLOW_WINDOWS_RETENTION_DAYS", 30, 3650
            ),
            phase7_stablecoin_context_retention_days=_bounded_positive_int(
                env, "PHASE7_STABLECOIN_CONTEXT_RETENTION_DAYS", 365, 3650
            ),
            phase7_ingestion_checkpoints_retention_days=_bounded_positive_int(
                env, "PHASE7_INGESTION_CHECKPOINTS_RETENTION_DAYS", 365, 3650
            ),
            phase7_enrichment_retention_days=_bounded_positive_int(
                env, "PHASE7_ENRICHMENT_RETENTION_DAYS", 365, 3650
            ),
        )
        settings.validate_startup()
        return settings
