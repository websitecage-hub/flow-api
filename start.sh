#!/usr/bin/env bash
set -e

# Persistent dirs (Render Disk mounted at /data)
mkdir -p "$FLOW_PROFILE" "$FLOW_OUT" /data/sessions /data/logs

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
