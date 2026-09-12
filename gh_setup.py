# -*- coding: utf-8 -*-
"""GitHub: verify token, ensure private flow-api repo exists. No secrets printed."""
import os, sys, json
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
tok = os.environ.get("GH_TOKEN", "")
print("token present:", bool(tok), flush=True)
from curl_cffi import requests as cffi
s = cffi.Session(impersonate="chrome120")
H = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
r = s.get("https://api.github.com/user", headers=H, timeout=30)
print("user:", r.status_code, r.text[:200].replace("\n", " "), flush=True)
r = s.get("https://api.github.com/repos/websitecage-hub/flow-api", headers=H, timeout=30)
print("repo:", r.status_code, flush=True)
if r.status_code == 200:
    j = r.json()
    print("private:", j.get("private"), "default_branch:", j.get("default_branch"), flush=True)
elif r.status_code == 404:
    print("repo missing; trying to create private repo...", flush=True)
    c = s.post("https://api.github.com/orgs/websitecage-hub/repos", headers=H,
               json={"name": "flow-api", "private": True, "auto_init": False,
                     "description": "Google Flow image API (split-brain)"}, timeout=30)
    print("create:", c.status_code, c.text[:300].replace("\n", " "), flush=True)
print("GH DONE", flush=True)
