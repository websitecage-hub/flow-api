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

# ── WARP STANDBY (off unless FLOW_WARP=1) ─────────────────────────────────
# When Render's egress IP is reCAPTCHA-flagged, this routes the browser through
# a Cloudflare WARP tunnel instead — verified working end-to-end (render done,
# 0 reCAPTCHA flags, +56s vs direct). wireproxy = userspace WireGuard->SOCKS5,
# so it needs no TUN/NET_ADMIN that Render free does not provide.
#   FLOW_WARP=1            start the tunnel (default: off)
#   WARP_LICENSE=...       optional: bind to your CF account (higher quota)
# The identity is registered at FIRST boot and persisted in the profile dir;
# later boots reuse it. FLOW_PROXY_URL is then pointed at the tunnel ONLY if
# the tunnel actually passes traffic — a proxy that swallows requests is worse
# than no proxy, so a failed egress check falls back to the direct egress.
if [ "${FLOW_WARP:-0}" = "1" ] && command -v wireproxy >/dev/null; then
  WARP_DIR="$FLOW_PROFILE/warp"
  mkdir -p "$WARP_DIR"
  if [ ! -f "$WARP_DIR/wgcf-profile.conf" ]; then
    echo "[start][warp] registering free WARP identity"
    (cd "$WARP_DIR" && wgcf register --accept-tos >/dev/null 2>&1 \
       && wgcf update >/dev/null 2>&1 \
       && [ -n "${WARP_LICENSE:-}" ] && wgcf update --license "$WARP_LICENSE" >/dev/null 2>&1 \
       || true)
    if [ -f "$WARP_DIR/wgcf-account.toml" ] && command -v wgcf >/dev/null; then
      (cd "$WARP_DIR" && wgcf generate >/dev/null 2>&1 || true)
    fi
  fi
  if [ -f "$WARP_DIR/wgcf-profile.conf" ]; then
    # wireproxy v1.0.9 wants BindAddress as host:port (the old host+Port split
    # panics: "missing port in address" — hit on the dev box).
    printf 'WGConfig = %s/wgcf-profile.conf\n\n[Socks5]\nBindAddress = 127.0.0.1:1080\n' \
      "$WARP_DIR" > "$WARP_DIR/warp.ini"
    wireproxy -c "$WARP_DIR/warp.ini" > "$WARP_DIR/wireproxy.log" 2>&1 &
    WPID=$!
    sleep 8
    WARP_IP=$(curl -fsS --max-time 20 --socks5-hostname 127.0.0.1:1080 \
      https://api.ipify.org 2>/dev/null || true)
    if [ -n "$WARP_IP" ]; then
      export FLOW_PROXY_URL="socks5://127.0.0.1:1080"
      echo "[start][warp] tunnel UP, egress $WARP_IP — browser routed via WARP"
    else
      echo "[start][warp] tunnel did NOT pass traffic (log: $WARP_DIR/wireproxy.log)" \
           "— falling back to direct egress"
      kill "$WPID" 2>/dev/null || true
    fi
  else
    echo "[start][warp] registration failed — direct egress"
  fi
fi

# SIGTERM handler: Render sends SIGTERM to PID 1 during deploys. The browser
# subprocess can outlive python and stall the container swap -> update_failed.
# Trap it: kill chrome, then exit immediately so the new container takes over.
cleanup() {
  pkill -9 -f chrome-headless-shell 2>/dev/null
  pkill -9 -f "flow_server.py" 2>/dev/null
  pkill -9 -f "wireproxy" 2>/dev/null
  exit 0
}
trap cleanup SIGTERM SIGINT

python -u flow_server.py --host 0.0.0.0 --port "${PORT:-8787}" &
SERVER_PID=$!
# forward SIGTERM to the python process (which re-traps and exits fast)
wait $SERVER_PID

