# -*- coding: utf-8 -*-
"""Poll Render deploy until live, then smoke-test the hosted API."""
import os, sys, time, json
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
H = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
SID = "srv-daifp4rm8hqs73cm70g0"
for i in range(40):
    r = s.get(f"https://api.render.com/v1/services/{SID}/deploys?limit=1", headers=H, timeout=30)
    d = r.json()[0]["deploy"]
    print(f"[{i}] {d['status']} {d.get('finishedAt', '')}", flush=True)
    if d["status"] == "live":
        break
    if d["status"] in ("build_failed", "update_failed", "deploys_failed", "canceled"):
        print("FAILED:", json.dumps(d)[:800], flush=True)
        break
    time.sleep(30)
print("DEPLOY:", d["status"], flush=True)
# smoke test (no key needed for GETs)
for p in ("/health", "/credits"):
    try:
        r = s.get("https://flow-api-znj4.onrender.com" + p, timeout=90)
        print(p, r.status_code, r.text[:250].replace("\n", " "), flush=True)
    except Exception as e:
        print(p, "ERR", str(e)[:150], flush=True)
print("SMOKE DONE", flush=True)
