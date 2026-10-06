"""Runtime configuration for the canonical-data paper readiness monitor."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from quant_data_layer.freshness import FRESHNESS_POLICY


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    dsn: str | None
    state_db: Path
    poll_seconds: float
    symbols: tuple[str, ...]
    project_root: Path
    # These are the realtime Stage1/Funding hard limits, not a global data TTL.
    ticker_max_age_seconds: int = int(FRESHNESS_POLICY["PRICE_STAGE1"].hard_seconds)
    collector_max_age_seconds: int = 30
    funding_max_age_seconds: int = int(FRESHNESS_POLICY["FUNDING"].hard_seconds)
    ticker_soft_max_age_seconds: int = int(FRESHNESS_POLICY["PRICE_STAGE1"].soft_seconds)
    funding_soft_max_age_seconds: int = int(FRESHNESS_POLICY["FUNDING"].soft_seconds)
    build_revision: str | None = None
    policy_manifest_path: Path | None = None
    policy_approval_path: Path | None = None
    risk_policy_path: Path | None = None
    execution_profile_path: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeConfig":
        values = os.environ if env is None else env
        root = Path(values.get("QUANT_PROJECT_ROOT", str(PROJECT_ROOT))).resolve()
        raw_symbols = values.get("QUANT_REALTIME_PAPER_SYMBOLS", ",".join(DEFAULT_SYMBOLS))
        symbols = tuple(item.strip().upper() for item in raw_symbols.split(",") if item.strip())
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("QUANT_REALTIME_PAPER_SYMBOLS must be a unique non-empty list")
        try:
            poll_seconds = float(values.get("QUANT_REALTIME_PAPER_POLL_SECONDS", "15"))
        except ValueError as exc:
            raise ValueError("QUANT_REALTIME_PAPER_POLL_SECONDS must be a positive number") from exc
        if not 2 <= poll_seconds <= 300:
            raise ValueError("QUANT_REALTIME_PAPER_POLL_SECONDS must be between 2 and 300")
        state_dir = Path(values.get("QUANT_REALTIME_PAPER_STATE_DIR", str(root / "var" / "realtime-paper"))).resolve()
        dsn = values.get("QUANT_REALTIME_PAPER_DSN") or values.get("POSTGRES_DSN") or None
        def configured_path(name: str, default: str | None = None) -> Path | None:
            raw = values.get(name, default)
            if not raw:
                return None
            path = Path(raw)
            return path.resolve() if path.is_absolute() else (root / path).resolve()
        policy_manifest_path = configured_path(
            "PHASE9_POLICY_MANIFEST_PATH", "policies/phase9_policy_v1.json",
        )
        policy_approval_path = configured_path(
            "PHASE9_POLICY_APPROVAL_PATH", "policies/phase9_policy_v1.approval.json",
        )
        risk_policy_path = configured_path("QUANT_REALTIME_PAPER_RISK_POLICY_PATH")
        execution_profile_path = configured_path("QUANT_REALTIME_PAPER_EXECUTION_PROFILE_PATH")
        try:
            funding_max_age = int(values.get("QUANT_REALTIME_PAPER_FUNDING_MAX_AGE_SECONDS", str(FRESHNESS_POLICY["FUNDING"].hard_seconds)))
        except ValueError as exc:
            raise ValueError("QUANT_REALTIME_PAPER_FUNDING_MAX_AGE_SECONDS must be an integer") from exc
        if not 1 <= funding_max_age <= 86400:
            raise ValueError("QUANT_REALTIME_PAPER_FUNDING_MAX_AGE_SECONDS must be between 1 and 86400")
        funding_max_age = min(funding_max_age, int(FRESHNESS_POLICY["FUNDING"].hard_seconds))
        return cls(
            dsn, state_dir / "sessions.sqlite3", poll_seconds, symbols, root,
            funding_max_age_seconds=funding_max_age,
            build_revision=values.get("QUANT_BUILD_REVISION") or None,
            policy_manifest_path=policy_manifest_path,
            policy_approval_path=policy_approval_path,
            risk_policy_path=risk_policy_path,
            execution_profile_path=execution_profile_path,
        )

    @property
    def config_hash(self) -> str:
        public_settings = {
            "mode": "REALTIME_PAPER",
            "symbols": self.symbols,
            "poll_seconds": self.poll_seconds,
            "ticker_soft_max_age_seconds": self.ticker_soft_max_age_seconds,
            "ticker_max_age_seconds": self.ticker_max_age_seconds,
            "collector_max_age_seconds": self.collector_max_age_seconds,
            "funding_soft_max_age_seconds": self.funding_soft_max_age_seconds,
            "funding_max_age_seconds": self.funding_max_age_seconds,
        }
        return hashlib.sha256(json.dumps(public_settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
