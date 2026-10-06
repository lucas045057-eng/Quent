"""Offline, fixture-only deterministic Phase 9 replay command."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from quant_phase9.canonical import canonical_json
from quant_phase9.replay import load_phase9_replay_manifest, replay_phase9


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path,
        default=ROOT / "tests/fixtures/phase9/manifest.json",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args(argv)
    bundle = load_phase9_replay_manifest(args.manifest, project_root=ROOT)
    result = replay_phase9(bundle)
    payload = canonical_json(result) + "\n"
    destination = args.json_output or args.output
    if args.json_output is not None and args.output is not None:
        parser.error("choose one output path")
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
