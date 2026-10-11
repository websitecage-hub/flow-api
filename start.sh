#!/usr/bin/env bash
# Render free-tier entrypoint. Finds the light browser, seeds the login, and
# starts flow_server.py (which boots the chrome-headless-shell engine inline).
set -e

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

exec python -u flow_server.py --host 0.0.0.0 --port "${PORT:-8787}"
