#!/usr/bin/env bash
set -euo pipefail
action="${1:?status or stop required}"
repo="${2:?project path required}"
if [[ "$action" != "status" && "$action" != "stop" && "$action" != "acceptance" ]]; then
  echo "Unsupported control action: $action" >&2
  exit 2
fi
cd "$repo"
repo="$(pwd -P)"
export PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}"
python_bin="${QUANT_REALTIME_PAPER_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  for candidate in "$repo/.venv/bin/python" "${repo}-env/bin/python" "$(command -v python3 || true)"; do
    if [[ -x "$candidate" ]] && "$candidate" -c "import quant_realtime_paper.cli" >/dev/null 2>&1; then
      python_bin="$candidate"
      break
    fi
  done
fi
if [[ -z "$python_bin" ]]; then
  echo "Could not find a Python environment with quant_realtime_paper installed." >&2
  exit 3
fi
if [[ "$action" == "acceptance" ]]; then
  duration="${3:-24h}"
  exec "$python_bin" -m quant_realtime_paper acceptance --duration "$duration"
fi
exec "$python_bin" -m quant_realtime_paper "$action"