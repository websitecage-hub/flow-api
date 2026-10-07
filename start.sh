#!/usr/bin/env bash
set -e

# Persistent dirs (Render Disk mounted at /data)
mkdir -p "$FLOW_PROFILE" "$FLOW_OUT" /data/sessions /data/logs

# Locate the browser binary and export it, so flow_api's engine never has to
# guess the path. FLOW_ENGINE=headless -> the tiny single-process
# chrome-headless-shell (~120 MB, fits Render free 512 MB); else full Chromium.
if [ "${FLOW_ENGINE}" = "headless" ]; then
  CHROME_BIN=$(find /root/.cache/ms-playwright /ms-playwright -name chrome-headless-shell -type f 2>/dev/null | head -1)
else
  CHROME_BIN=$(find /root/.cache/ms-playwright /ms-playwright -name chrome -type f 2>/dev/null | head -1)
fi
if [ -z "$CHROME_BIN" ]; then
  CHROME_BIN=$(find /root/.cache/ms-playwright /ms-playwright -name headless_shell -type f 2>/dev/null | head -1)
fi
if [ -n "$CHROME_BIN" ]; then
  export FLOW_CHROME="$CHROME_BIN"
  echo "[start] using chromium: $CHROME_BIN"
else
  echo "[start] WARNING: no chromium binary found"
fi

# One-time seed: if a profile tarball URL is provided and the profile dir is
# empty, download + extract it so you don't have to log in by hand again.
if [ -n "$PROFILE_TAR_URL" ] && [ ! -d "$FLOW_PROFILE/Default" ]; then
  echo "[start] seeding profile from $PROFILE_TAR_URL"
  curl -fsSL "$PROFILE_TAR_URL" -o /tmp/profile.tar.gz \
    && tar -xzf /tmp/profile.tar.gz -C "$FLOW_PROFILE" \
    && echo "[start] profile seeded" \
    || echo "[start] profile seed failed; will try auto-login"
fi

exec python -u flow_server.py --host 0.0.0.0 --port "${PORT:-8787}"
