#!/usr/bin/env bash
# Start the options-book UI on http://127.0.0.1:8117
#
# Uses a local .venv, creating it on first run. A workspace venv that already
# has the deps is used instead when present, so this stays cheap in a monorepo.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8117}"
HOST="${HOST:-127.0.0.1}"

pick_python() {
  for cand in "../.venv/bin/python" ".venv/bin/python" "$(command -v python3 || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, openpyxl
PY
    then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  echo "· creating local .venv"
  python3 -m venv .venv
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
  PY=".venv/bin/python"
fi

echo "· portfolio-ui → http://${HOST}:${PORT}  (python: ${PY})"
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level warning
