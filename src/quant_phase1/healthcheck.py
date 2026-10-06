"""Container healthcheck: fail closed unless the local runtime is RUNNING."""

from __future__ import annotations

import argparse
from pathlib import Path


def check(component: str) -> int:
    path = Path(f"/tmp/{component}.health")
    try:
        state = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return 1
    return 0 if state == "RUNNING" else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", required=True)
    raise SystemExit(check(parser.parse_args().component))


if __name__ == "__main__":
    main()
