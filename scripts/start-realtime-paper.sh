#!/usr/bin/env bash
set -euo pipefail

duration="${1:-24h}"
repo="${2:?project path required}"
cd "$repo"
repo="$(pwd -P)"

# Reuse only the existing local stack. Never create replacement data services here.
for service in quant-postgres quant-collector; do
  if ! docker inspect "$service" >/dev/null 2>&1; then
    echo "Existing Docker service is missing: $service. Restore it with the project compose file first." >&2
    exit 2
  fi
  status="$(docker inspect --format '{{.State.Status}}' "$service")"
  if [[ "$status" != "running" ]]; then
    docker start "$service" >/dev/null
  fi
done

for _ in $(seq 1 60); do
  if docker exec quant-postgres pg_isready -U quant -d quant >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if ! docker exec quant-postgres pg_isready -U quant -d quant >/dev/null 2>&1; then
  echo "Existing canonical PostgreSQL is not ready." >&2
  exit 3
fi

dsn="${QUANT_REALTIME_PAPER_DSN:-${POSTGRES_DSN:-}}"
if [[ -z "$dsn" ]]; then
  dsn="$(docker inspect quant-collector --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^POSTGRES_DSN=//p' | head -n 1)"
fi
if [[ -z "$dsn" ]]; then
  echo "Set QUANT_REALTIME_PAPER_DSN or configure POSTGRES_DSN on the existing collector." >&2
  exit 4
fi
postgres_ip="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' quant-postgres)"
if [[ -z "$postgres_ip" ]]; then
  echo "Could not resolve the existing PostgreSQL container address." >&2
  exit 5
fi
if [[ "$dsn" == *"@postgres:"* ]]; then
  dsn="${dsn/@postgres:/@$postgres_ip:}"
elif [[ "$dsn" == *"@quant-postgres:"* ]]; then
  dsn="${dsn/@quant-postgres:/@$postgres_ip:}"
fi
export QUANT_REALTIME_PAPER_DSN="$dsn"

# Wait for the existing Phase1 collector heartbeat in canonical PostgreSQL.
for _ in $(seq 1 60); do
  status="$(docker exec quant-postgres psql -U quant -d quant -tAc "SELECT status FROM system_health WHERE component='quant-collector' ORDER BY checked_at DESC LIMIT 1" 2>/dev/null || true)"
  if [[ "$status" == "AVAILABLE" ]]; then
    break
  fi
  sleep 2
done
if [[ "$status" != "AVAILABLE" ]]; then
  echo "Existing Phase1 collector has not reported a healthy heartbeat." >&2
  exit 6
fi

export PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}"
python_bin="${QUANT_REALTIME_PAPER_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  for candidate in "$repo/.venv/bin/python" "${repo}-env/bin/python" "$(command -v python3 || true)"; do
    if [[ -x "$candidate" ]] && "$candidate" -c "import quant_realtime_paper.runtime" >/dev/null 2>&1; then
      python_bin="$candidate"
      break
    fi
  done
fi
if [[ -z "$python_bin" ]]; then
  echo "Could not find a Python environment with quant_realtime_paper installed." >&2
  exit 7
fi
"$python_bin" -m quant_realtime_paper start --duration "$duration"