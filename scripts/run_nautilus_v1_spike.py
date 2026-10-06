#!/usr/bin/env python3
"""Run the local pinned-engine fixture, emitting measured machine-readable data."""

from __future__ import annotations

import json
import resource
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from quant_nautilus.spike import run_fixture_spike  # noqa: E402


def _json(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unexpected result type {type(value).__name__}")


def main() -> int:
    result = run_fixture_spike()
    output = asdict(result)
    output["peak_rss_mib"] = round(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3,
    )
    output["engine_cap_mib"] = 384
    output["engine_peak_under_cap"] = output["peak_rss_mib"] <= 384
    print(json.dumps(output, default=_json, sort_keys=True, separators=(",", ":")))
    return 0 if len(result.fills) == 2 and output["engine_peak_under_cap"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
