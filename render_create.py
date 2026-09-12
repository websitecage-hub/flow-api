# -*- coding: utf-8 -*-
"""Create flow-api web service on Render (Docker, starter, oregon)."""
import os, sys, json, secrets
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flow_api import _load_dotenv  # noqa: E402
_load_dotenv()
key = os.environ.get("RENDER_API_KEY", "")
cms = "cms1:" + secrets.token_hex(16)
print("CMS_KEY_FOR_USER", cms, flush=True)
from curl_cffi import requests as cffi
s = cffi.Session(impersonate="chrome120")
H = {"Authorization": f"Bearer {key}", "Accept": "application/json",
     "Content-Type": "application/json"}
payload = {
    "type": "web_service",
    "name": "flow-api",
    "ownerId": "tea-d6tfmmvkijhs73f477ng",
    "repo": "https://github.com/websitecage-hub/flow-api",
    "branch": "main",
    "autoDeploy": "yes",
    "serviceDetails": {
        "runtime": "docker",
        "dockerfilePath": "./Dockerfile",
        "healthCheckPath": "/health",
            "plan": "free",
        "region": "oregon",
        "envVars": [
            {"key": "ROLE", "value": "api"},
            {"key": "FLOW_PROJECT", "value": "4124a6cc-e936-4354-b569-b5a08fdfec0b"},
            {"key": "FLOW_MODEL", "value": "NARWHAL"},
            {"key": "API_KEYS", "value": cms},
            {"key": "RATE_POST_PER_MIN", "value": "20"},
            {"key": "RATE_GET_PER_MIN", "value": "120"},
        ],
    },
}
r = s.post("https://api.render.com/v1/services", headers=H,
           json=payload, timeout=60)
print("create:", r.status_code, flush=True)
print(r.text[:1500].replace("\n", " "), flush=True)
open("reports/render_create.json", "w", encoding="utf-8").write(r.text)
print("SVC DONE", flush=True)
