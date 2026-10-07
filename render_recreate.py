# -*- coding: utf-8 -*-
"""Recreate service as native Python runtime (no Docker)."""
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
r = s.delete(f"https://api.render.com/v1/services/{SID}", headers=H, timeout=60)
print("delete:", r.status_code, r.text[:200].replace("\n", " "), flush=True)
payload = {
    "type": "web_service",
    "name": "flow-api",
    "ownerId": "tea-d6tfmmvkijhs73f477ng",
    "repo": "https://github.com/websitecage-hub/flow-api",
    "branch": "main",
    "autoDeploy": "yes",
    "serviceDetails": {
        "runtime": "python",
        "plan": "free",
        "region": "oregon",
        "healthCheckPath": "/health",
        "envSpecificDetails": {
            "buildCommand": "pip install -r requirements.docker.txt",
            "startCommand": "python -u flow_server.py --host 0.0.0.0 --port $PORT",
            "pythonVersion": "3.11.0",
        },
        "envVars": [
            {"key": "ROLE", "value": "api"},
            {"key": "FLOW_PROJECT", "value": "4124a6cc-e936-4354-b569-b5a08fdfec0b"},
            {"key": "FLOW_MODEL", "value": "NARWHAL"},
            {"key": "API_KEYS", "value": os.environ.get("FLOW_API_KEYS", "")},
            {"key": "RATE_POST_PER_MIN", "value": "20"},
            {"key": "RATE_GET_PER_MIN", "value": "120"},
        ],
    },
}
r = s.post("https://api.render.com/v1/services", headers=H, json=payload, timeout=60)
print("create:", r.status_code, flush=True)
print(r.text[:1200].replace("\n", " "), flush=True)
open("reports/render_recreate.json", "w", encoding="utf-8").write(r.text)
print("RECREATE DONE", flush=True)
