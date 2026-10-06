"""Frozen Phase 9 V1 resource ceilings and strict environment loading."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


_CEILINGS = {
    "max_inflight_evaluations": 2,
    "max_queued_ids": 32,
    "max_queued_bytes": 8 * 1024 * 1024,
    "evaluation_timeout_seconds": 30,
    "max_concurrent_jev_calls": 1,
    "max_revalidations_per_minute": 16,
}


@dataclass(frozen=True, slots=True)
class Phase9RuntimeConfig:
    enabled: bool = False
    max_inflight_evaluations: int = 2
    max_queued_ids: int = 32
    max_queued_bytes: int = 8 * 1024 * 1024
    evaluation_timeout_seconds: int = 30
    max_concurrent_jev_calls: int = 1
    max_revalidations_per_minute: int = 16
    stage1_candidate_ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be boolean")
        ttl = self.stage1_candidate_ttl_seconds
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
            raise ValueError("stage1_candidate_ttl_seconds must be a positive integer or None")
        for name, ceiling in _CEILINGS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be an integer from 1 through {ceiling}")


def load_phase9_runtime_config(env: Mapping[str, str] | None = None) -> Phase9RuntimeConfig:
    values = os.environ if env is None else env
    raw_enabled = values.get("PHASE9_ENABLED", "0")
    if raw_enabled not in {"0", "1"}:
        raise ValueError("PHASE9_ENABLED must be 0 or 1")
    settings: dict[str, object] = {"enabled": raw_enabled == "1"}
    raw_stage1_ttl = values.get("PHASE9_STAGE1_CANDIDATE_TTL_SECONDS")
    if raw_stage1_ttl is None or raw_stage1_ttl == "":
        settings["stage1_candidate_ttl_seconds"] = None
    else:
        if not raw_stage1_ttl.isascii() or not raw_stage1_ttl.isdecimal() or int(raw_stage1_ttl) <= 0:
            raise ValueError("PHASE9_STAGE1_CANDIDATE_TTL_SECONDS must be a positive decimal integer")
        settings["stage1_candidate_ttl_seconds"] = int(raw_stage1_ttl)
    for name, default in _CEILINGS.items():
        env_name = "PHASE9_" + name.upper()
        raw = values.get(env_name, str(default))
        if not raw.isascii() or not raw.isdecimal():
            raise ValueError(f"{env_name} must be a positive decimal integer")
        settings[name] = int(raw)
    return Phase9RuntimeConfig(**settings)
