# -*- coding: utf-8 -*-
"""Set Render env vars explicitly, then trigger redeploy."""
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
vars_ = [
    {"key": "ROLE", "value": "api"},
    {"key": "FLOW_PROJECT", "value": "4124a6cc-e936-4354-b569-b5a08fdfec0b"},
    {"key": "FLOW_MODEL", "value": "NARWHAL"},
    {"key": "API_KEYS", "value": "cms1:c2373760c1a44c761f47b489fa847cc5"},
    {"key": "RATE_POST_PER_MIN", "value": "20"},
    {"key": "RATE_GET_PER_MIN", "value": "120"},
]
r = s.put(f"https://api.render.com/v1/services/{SID}/env-vars", headers=H,
          json=vars_, timeout=60)
print("put env:", r.status_code, r.text[:500].replace("\n", " "), flush=True)
print("ENVSET DONE", flush=True)
