FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ROLE=all \
    FLOW_ENGINE=headless \
    FLOW_PROFILE=/app/profile-copy \
    FLOW_OUT=/app/outputs \
    FLOW_MEM_GUARD_MB=420

WORKDIR /app

# Install python deps + the LIGHT headless browser (chrome-headless-shell).
# This is the ~120MB single-process browser that drives the composer and
# mints reCAPTCHA tokens — it fits Render free 512MB.
COPY requirements.docker.txt .
RUN pip install --no-cache-dir -r requirements.docker.txt \
    && playwright install --with-deps chromium-headless-shell

# Engine + server + all deps. flow_api.py (full-Chrome) stays for compat;
# FLOW_ENGINE=headless selects the light one.
COPY flow_api.py flow_headless.py flow_api_headless.py flow_server.py \
     flow_store.py worker.py gen.py menu.py flow.py ./
COPY start.sh ./

# Cookie/session import tools (the deploy itself gets its signed-in session
# from the FLOW_COOKIES_JSON env var — never committed to the repo)
COPY import_cookies.py ./

RUN chmod +x start.sh && mkdir -p /app/profile-copy /app/outputs /app/sessions /app/logs /app/ui
# copy the test bench UI
COPY ui/ ./ui/

EXPOSE 8787
# start.sh: finds the headless-shell binary, seeds profile from PROFILE_TAR_URL,
# then runs flow_server.py (which now boots the light engine inline, ROLE=all)
CMD ["./start.sh"]