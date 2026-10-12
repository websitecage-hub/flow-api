#!/usr/bin/env bash
# Render free-tier entrypoint. Finds the light browser, seeds the login, and
# starts flow_server.py (which boots the chrome-headless-shell engine inline).
set -e

# MEM: the playwright node driver silently eats ~120MB. Cap its V8 heap so
# it stays lean — it only shuffles CDP messages, it never needs more.
export NODE_OPTIONS="${NODE_OPTIONS:-} --max-old-space-size=96"

export FLOW_ENGINE="${FLOW_ENGINE:-headless}"
FLOW_PROFILE="${FLOW_PROFILE:-/app/profile-copy}"
FLOW_OUT="${FLOW_OUT:-/app/outputs}"
cd "$(dirname "$0")"

mkdir -p "$FLOW_PROFILE" "$FLOW_OUT" sessions logs outputs

# Locate the browser binary. FLOW_ENGINE=headless -> the tiny single-process
# chrome-headless-shell (~128 MB, fits Render free 512 MB); else full Chromium.
if [ -z "${FLOW_CHROME:-}" ]; then
  if [ "$FLOW_ENGINE" = "headless" ]; then
    FLOW_CHROME=$(find /root/.cache/ms-playwright /ms-playwright "$HOME/.cache/ms-playwright" \
        -type f -name chrome-headless-shell 2>/dev/null | head -1)
  fi
  [ -z "${FLOW_CHROME:-}" ] && \
    FLOW_CHROME=$(find /root/.cache/ms-playwright /ms-playwright "$HOME/.cache/ms-playwright" \
        -type f -name chrome 2>/dev/null | head -1)
fi
if [ -n "${FLOW_CHROME:-}" ]; then
  export FLOW_CHROME
  echo "[start] browser: $FLOW_CHROME"
else
  echo "[start] WARNING: no browser binary found"
fi

# One-time seed: if PROFILE_TAR_URL is set and the profile is empty, unpack it
# (free tier has no persistent disk, so the login must be re-seeded each boot).
if [ -n "${PROFILE_TAR_URL:-}" ] && [ ! -d "$FLOW_PROFILE/Default" ]; then
  echo "[start] seeding profile from PROFILE_TAR_URL"
  curl -fsSL "$PROFILE_TAR_URL" -o /tmp/profile.tar.gz \
    && tar -xzf /tmp/profile.tar.gz -C "$FLOW_PROFILE" \
    && echo "[start] profile seeded" \
    || echo "[start] profile seed failed"
fi

# SIGTERM handler: Render sends SIGTERM to PID 1 during deploys. The browser
# subprocess can outlive python and stall the container swap -> update_failed.
# Trap it: kill chrome, then exit immediately so the new container takes over.
cleanup() {
  pkill -9 -f chrome-headless-shell 2>/dev/null
  pkill -9 -f "flow_server.py" 2>/dev/null
  exit 0
}
trap cleanup SIGTERM SIGINT

python -u flow_server.py --host 0.0.0.0 --port "${PORT:-8787}" &
SERVER_PID=$!
# forward SIGTERM to the python process (which re-traps and exits fast)
wait $SERVER_PID

