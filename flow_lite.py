#!/usr/bin/env python3
"""Lightweight, pure-HTTP Google Flow API — no browser, no PC worker.

What works with plain requests (curl_cffi chrome impersonation):
  GET /health   GET /history   GET /credits   GET /projects
  GET /media/<uuid>           GET /options

What CANNOT work without a real browser (reCAPTCHA Enterprise gate):
  POST /generate, POST /upscale  -> always 403 PUBLIC_ERROR_UNUSUAL_ACTIVITY
  (kept as stubs that return a clear 501 so callers get a real error, not a hang)

Run:  python flow_lite.py --port 8787
      FLOW_PROJECT=<uuid> python flow_lite.py
"""
import argparse, json, os, re, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
STATE = ROOT / "sessions" / "flow_api_state.json"
COOKIES = ROOT / "sessions" / "flow.cookies.full.json"
FLOW = "https://flow.google.com"
BE = FLOW + "/u/1/_/AiSandboxAngularFrontend/data/batchexecute"

def _load_dotenv():
    try:
        for ln in (ROOT / ".env").read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except Exception:
        pass
_load_dotenv()

PID = os.environ.get("FLOW_PROJECT", "") or json.loads(STATE.read_text()).get("project_id", "") if STATE.exists() else os.environ.get("FLOW_PROJECT", "")
KEYS = {k.split(":",1)[1].strip(): k.split(":",1)[0].strip()
        for k in os.environ.get("API_KEYS","").split(",") if ":" in k}

def _ses():
    from curl_cffi import requests as c
    s = c.Session(impersonate="chrome146")
    if COOKIES.exists():
        try:
            for c in json.loads(COOKIES.read_text()):
                if "google.com" in c.get("domain",""):
                    s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path","/"))
        except Exception:
            pass
    return s

def _at(pid):
    r = _ses().get(FLOW + f"/u/1/project/{pid}", timeout=30)
    m = re.search(rb"AIQ-[A-Za-z0-9_\-]{20,80}:\d{10,20}", r.content)
    return m.group(0).decode() if m else ""

def rpc(rpcid, payload, pid):
    for attempt in range(2):
        at = _at(pid)
        if not at: return None
        body = f'f.req={json.dumps([[[rpcid, json.dumps(payload), None, "generic"]]])}&at={at}'
        r = _ses().post(BE + f"?rpcids={rpcid}&source-path=" + urllib.parse.quote(f"/u/1/project/{pid}", safe=""),
                        data=body, headers={"content-type":"application/x-www-form-urlencoded","origin":FLOW,"referer":FLOW+f"/u/1/project/{pid}"}, timeout=60)
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.startswith('[["wrb.fr"'):
                    try:
                        d = json.loads(line)
                        if d and d[0] and len(d[0]) > 2 and d[0][0] == "wrb.fr" and d[0][1] == rpcid:
                            return json.loads(d[0][2]) if d[0][2] else None
                    except Exception:
                        continue
        time.sleep(1)
    return None

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _send(self, code, obj, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
        self.wfile.write(b)
    def _auth(self):
        a = self.headers.get("Authorization","")
        return not KEYS or (a.startswith("Bearer ") and a[7:].strip() in KEYS)
    def log_message(self, *a): pass
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/health":
            self._send(200, {"ok": True, "project": PID, "cookies": COOKIES.exists()})
        elif p == "/credits":
            d = rpc("nzlxg", [], PID)
            self._send(200, {"ok": True, "credits": d[0] if d else None, "raw": d})
        elif p == "/history":
            d = rpc("Zzl0ze", [f"projects/{PID}", None, None, None, [1]], PID)
            self._send(200, {"ok": True, "data": d})
        elif p == "/options":
            d = rpc("ngNC2", [f"tools/PINHOLE/projects/{PID}"], PID)
            self._send(200, {"ok": True, "data": d})
        elif p.startswith("/media/"):
            self._send(404, {"ok": False, "error": "use signed url from /history"})
        else:
            self._send(404, {"ok": False, "endpoints": ["/health","/credits","/history","/options"]})
    def do_POST(self):
        if not self._auth():
            return self._send(401, {"ok": False, "error": "bad key"})
        p = urllib.parse.urlparse(self.path).path
        if p in ("/generate", "/upscale"):
            self._send(501, {"ok": False, "error": "reCAPTCHA-gated: needs a real browser, not pure requests. Use the hosted Docker deployment (ROLE=all) instead."})
        else:
            self._send(404, {"ok": False})

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    a = ap.parse_args()
    print(f"flow-lite (requests-only) on http://{a.host}:{a.port}  project={PID[:13]}")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
