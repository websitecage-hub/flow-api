#!/bin/sh
# First-run bootstrap + REPL for macOS/Linux.
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "[*] first run: creating isolated env..."
  python3 -m venv .venv || exit 1
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python -u flow.py "$@"
