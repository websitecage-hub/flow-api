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
RUN pip install --no-cache-dir -r requirements.docker.txt \
    && playwright install --with-deps chromium-headless-shell \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       fontconfig fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
# NOTE: fontconfig/fonts are NOT optional. Without a working font config the
# renderer aborts on the Flow project page with
#   FATAL:third_party/skia/.../SkFontMgr_FontConfigInterface.cpp: Not implemented
# (observed directly). `--with-deps` usually pulls them; pinning them here
# makes the image deterministic.

# App code (engine + server + store + CLI). flow_api.py (full-Chrome) stays
# for compat but is never the engine under FLOW_ENGINE=headless.
COPY flow_api.py flow_headless.py flow_api_headless.py flow_server.py \
     flow_store.py worker.py gen.py menu.py flow.py ./
COPY import_cookies.py ./
COPY start.sh ./
COPY ui/ ./ui/

RUN chmod +x start.sh \
    && mkdir -p /app/profile-copy /app/outputs /app/sessions /app/logs /app/ui

EXPOSE 8787
CMD ["./start.sh"]
