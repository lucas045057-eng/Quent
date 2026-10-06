#!/usr/bin/env python3
"""Run one fixed Phase 9 compatibility suite and write machine evidence."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quant_phase9.compatibility_gate import suite_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(suite_main(repo_root=REPO_ROOT))
