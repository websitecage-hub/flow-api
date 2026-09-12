# -*- coding: utf-8 -*-
"""Inspect + fix Render env vars (FLOW_PROJECT missing?)."""
import os, sys, json
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flow_api import _load_dotenv  # noqa: E402
_load_dotenv()
key = os.environ.get("RENDER_API_KEY", "")
from curl_cffi import requests as cffi
s = cffi.Session(impersonate="chrome120")
H = {"Authorization": f"Bearer {key}", "Accept": "application/json",
     "Content-Type": "application/json"}
SID = "srv-daifp4rm8hqs73cm70g0"
r = s.get(f"https://api.render.com/v1/services/{SID}/env-vars", headers=H, timeout=30)
print("env list:", r.status_code, flush=True)
print(r.text[:2000].replace("\n", " "), flush=True)
print("ENV DONE", flush=True)
