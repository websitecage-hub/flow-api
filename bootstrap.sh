#!/usr/bin/env bash
# Clone-anywhere bootstrap: sets up deps + browser and starts the Flow API.
#
#   git clone <repo> && cd flow-api && ./bootstrap.sh
#
# Env (optional):
#   FLOW_ENGINE=headless|chrome   (default headless = 120MB, reads only;
#                                  chrome = full browser, can generate)
#   FLOW_PROJECT=<uuid>           your Flow project
#   FLOW_COOKIES_JSON=<json|b64>  cookies (else uses sessions/flow.cookies.full.json)
#   API_KEYS=cms1:secret          API auth
#   PORT=8787
set -e
cd "$(dirname "$0")"

echo "==> Flow API bootstrap"

# 1. python venv
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "==> creating .venv"
  "$PY" -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
if [ -f requirements.txt ]; then
  echo "==> installing python deps"
  ./.venv/bin/pip install -q -r requirements.txt
fi

# 2. browser(s)
echo "==> installing browsers (playwright)"
./.venv/bin/playwright install chromium >/dev/null 2>&1 || true
# headless-shell for the light engine (comes with chromium usually)
./.venv/bin/playwright install chromium-headless-shell >/dev/null 2>&1 || true

# 3. sanity: find a browser
CH=$(find "$HOME/.cache/ms-playwright" /root/.cache/ms-playwright -name chrome-headless-shell -o -name chrome 2>/dev/null | head -1)
echo "==> browser: ${CH:-NOT FOUND (playwright install failed?)}"

# 4. launch
ENGINE=${FLOW_ENGINE:-headless}
export FLOW_ENGINE="$ENGINE"
export PORT=${PORT:-8787}
echo "==> starting flow_server (engine=$ENGINE, port=$PORT)"
exec ./.venv/bin/python -u flow_server.py --host 0.0.0.0 --port "$PORT"