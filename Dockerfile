# Flow API — SELF-CONTAINED on Render FREE tier (512 MB RAM).
#
# Runs flow_server.py with the chrome-headless-shell engine inline.
# MEASURED on Linux x86_64 with this engine:
#   87 MB idle  ·  128 MB with flow.google.com loaded  ·  1 process
# vs full Chromium (old engine): 692 MB -> 1.65 GB across 9 processes,
# which OOM-kills Render's free 512 MB plan.
#
# This is the ONE Dockerfile Render builds (render.yaml -> dockerfilePath).
# It installs ONLY `chromium-headless-shell`, never full Chromium.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ROLE=all \
    FLOW_ENGINE=headless \
    FLOW_PROFILE=/app/profile-copy \
    FLOW_OUT=/app/outputs \
    FLOW_MEM_GUARD_MB=420

WORKDIR /app

# 1) Python deps (cached layer), then 2) the light single-process browser.
# chromium-headless-shell is a SEPARATE browser from `chromium` in Playwright
# — installing `chromium` alone can leave the light binary absent, which makes
# the engine fall back to "no engine binary -> ROLE=api" (generation silently
# off). Pin the light one explicitly. Browsers land in the default
# /root/.cache/ms-playwright, which the engine's auto-detect already globs.
COPY requirements.docker.txt ./
# PIP AND BROWSER ARE SEPARATE LAYERS ON PURPOSE.
# The browser download is the flaky part: playwright's CDN intermittently
# fails ("Download failure, code=1") on Render's build machines, and when it
# was chained with && after pip, a flaky download killed the WHOLE build in
# ~20 seconds. It went unnoticed because the layer was CACHED — the first
# requirements.docker.txt edit since the last green build invalidated the
# cache and exposed it. Splitting the layers means a browser-download blip
# fails only THAT layer (and Render retries it) instead of the entire image,
# and pip keeps its own cache independently.
RUN pip install --no-cache-dir -r requirements.docker.txt
# The browser binary: retry inside the layer (CDN blips are transient), and
# keep the layer split so a failure here is isolated.
RUN for i in 1 2 3; do \
      playwright install --with-deps chromium-headless-shell && break; \
      echo "[build] playwright download attempt $i failed — retrying in 20s"; \
      sleep 20; \
    done \
    && playwright install chromium-headless-shell \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       fontconfig fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
# NOTE: fontconfig/fonts are NOT optional. Without a working font config the
# renderer aborts on the Flow project page with
#   FATAL:third_party/skia/.../SkFontMgr_FontConfigInterface.cpp: Not implemented
# (observed directly). `--with-deps` usually pulls them; pinning them here
# makes the image deterministic.

# 2) WARP STANDBY (dormant unless FLOW_WARP=1 at boot).
#    Render's shared egress IP can get flagged by reCAPTCHA
#    (PUBLIC_ERROR_UNUSUAL_ACTIVITY — measured live, recovered after rest).
#    The standby is a WARP tunnel: wgcf registers the free anonymous identity
#    and wireproxy turns it into a local SOCKS5 proxy — userspace WireGuard,
#    because Render free has no TUN device / NET_ADMIN for a real interface.
#    ~17 MB of static Go binaries, zero processes and zero RAM until
#    FLOW_WARP=1. Versions verified working together on the dev box.
# NOTE: python:3.11-slim has NO curl (this exact gap failed 4 deploys in a
# row on Render — /bin/sh: curl: not found). The apt line above runs before
# this one and leaves lists behind until the cleanup below, so install curl
# HERE in the same RUN that needs it, then clean up.
ARG WGCF_VERSION=2.2.22
ARG WIREPROXY_VERSION=1.0.9
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /usr/local/bin/wgcf \
      "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_amd64" \
    && curl -fsSL -o /tmp/wireproxy.tar.gz \
      "https://github.com/pufferffish/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz" \
    && tar -xzf /tmp/wireproxy.tar.gz -C /usr/local/bin wireproxy \
    && chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy \
    && rm -f /tmp/wireproxy.tar.gz \
    && /usr/local/bin/wireproxy --version \
    && rm -rf /var/lib/apt/lists/*
# NOTE: curl is deliberately NOT purged — start.sh needs it at runtime
# (PROFILE_TAR_URL seeding, the WARP tunnel egress check).

# App code (engine + server + store + CLI). flow_api.py (full-Chrome) stays
# for compat but is never the engine under FLOW_ENGINE=headless.
COPY flow_api.py flow_headless.py flow_api_headless.py flow_auth.py \
     flow_cdp.py flow_server.py flow_store.py worker.py gen.py menu.py flow.py ./
# Watermark removal, with the alpha mask embedded (Pillow only, no numpy)
COPY dewatermark_server.py ./
COPY import_cookies.py ./
COPY start.sh ./
COPY ui/ ./ui/

RUN chmod +x start.sh \
    && mkdir -p /app/profile-copy /app/outputs /app/sessions /app/logs /app/ui

EXPOSE 8787
CMD ["./start.sh"]
