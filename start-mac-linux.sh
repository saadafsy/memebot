#!/usr/bin/env bash
# Set up (first time) and start memebot in paper mode.  Usage: ./start-mac-linux.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
[ -f .env ] || cp .env.example .env
.venv/bin/python -m memebot init --data data
echo "Starting memebot. The panel address is printed below (default http://127.0.0.1:8787). Ctrl+C stops it."
exec .venv/bin/python -m memebot run --data data
