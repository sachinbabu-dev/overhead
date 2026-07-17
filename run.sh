#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python3"
STAMP="$VENV/.requirements.sha256"

if [ ! -x "$PY" ]; then
  echo "[run.sh] creating $VENV"
  python3 -m venv "$VENV"
fi

CURRENT="$(shasum -a 256 requirements.txt | awk '{print $1}')"
PREVIOUS="$(cat "$STAMP" 2>/dev/null || echo "")"
if [ "$CURRENT" != "$PREVIOUS" ]; then
  echo "[run.sh] installing requirements"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
  echo "$CURRENT" > "$STAMP"
fi

exec "$PY" "${1:-server.py}"
