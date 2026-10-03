#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Google Flow HTTP API server — same single-process engine, exposed as REST.

Start:   python flow_server.py [--host 127.0.0.1] [--port 8787]
        (or double-click flow-server.bat)

Endpoints (all JSON, CORS-open):
  GET  /health                    engine + project status
  POST /generate                  {"prompt": "...", "model": "NARWHAL",
                                   "aspect": "landscape_4_3", "seed": 123}
      -> {"ok": true, "uuid", "seed", "model", "width", "height", "size",
          "prompt", "file": "outputs/<uuid>.jpg"}
  POST /upscale                   {"mediaId": "<uuid>", "resolution": "2K"}
      -> {"ok": true, "source": "<uuid>", "file": "outputs/<upsampled>.jpg"}
  GET  /history                   list media in the current project
  GET  /media/<uuid>              raw image bytes (download)
  GET  /credits                   remaining credits + paygate tier
  GET  /projects                  list projects
  GET  /project                   current project id
  POST /project                   {"projectId": "..."}  switch project

Note: generation + upscale need the off-screen Chrome engine (reCAPTCHA gate,
see flow_api.py docstring). Everything else is pure HTTP. Requests that hit the
engine are serialized (one at a time).
"""
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import flow_api
from flow_api import FlowAPI, load_state, save_state

flow_api._load_dotenv()

# ── API auth + rate limits (for content systems) ──────────────
# API_KEYS="cms1:secret1,cms2:secret2" (or bare keys). Empty = open (local).
# POST limits are intentionally generous; the Google-facing throttle in the
# worker is what protects the account (MIN_SUBMIT_INTERVAL etc.).
def _api_keys():
    raw = os.environ.get("API_KEYS", "").strip()
    keys = {}
    if not raw:
        return keys
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, val = part.split(":", 1)
            keys[val.strip()] = name.strip() or "key"
        else:
            keys[part] = part[:6]
    return keys


API_KEYS = _api_keys()
RATE_POST_PER_MIN = int(os.environ.get("RATE_POST_PER_MIN", "20"))
RATE_GET_PER_MIN = int(os.environ.get("RATE_GET_PER_MIN", "120"))
_BUCKETS = {}
_BUCKETS_LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _identity(self):
        auth = self.headers.get("Authorization", "") or ""
        key = auth[7:].strip() if auth.startswith("Bearer ") else ""
        name = API_KEYS.get(key, "") if key else ""
        ip = self.client_address[0] if self.client_address else "?"
        return (name or key[:6] or "anon") + "@" + ip

    def _rate_ok(self, is_post):
        limit = RATE_POST_PER_MIN if is_post else RATE_GET_PER_MIN
        key = self._identity() + (":P" if is_post else ":G")
        now = time.time()
        with _BUCKETS_LOCK:
            ws, count = _BUCKETS.get(key, (now, 0))
            if now - ws >= 60:
                ws, count = now, 0
            count += 1
            _BUCKETS[key] = (ws, count)
            if count > limit:
                return False, int(60 - (now - ws)) + 1
        return True, 0

    def _auth_ok(self):
        """POST endpoints need a key when API_KEYS is configured."""
        if not API_KEYS:
            return True
        auth = self.headers.get("Authorization", "") or ""
        return auth.startswith("Bearer ") and auth[7:].strip() in API_KEYS

    def _gate(self, is_post, need_auth):
        ok, retry = self._rate_ok(is_post)
        if not ok:
            self._send(429, {"ok": False, "error": "rate limited"},
                       headers={"Retry-After": str(retry)})
            return False
        if need_auth and not self._auth_ok():
            self._send(401, {"ok": False, "error": "missing/invalid API key"})
            return False
        return True

    def _send(self, code, obj, ctype="application/json", headers=None):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        if not isinstance(obj, bytes):
            ctype = "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_json(self):
        ln = int(self.headers.get("Content-Length") or 0)
        if ln <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(ln).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, *a):
        pass

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def do_GET(self):
        if not self._gate(is_post=False, need_auth=False):
            return
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        st = load_state()
        if p.path == "/health":
            try:
                self._send(200, api.status())
            except Exception as e:
                alive = bool(getattr(api, "engine", None))
                self._send(200, {"ok": False, "engine_alive": alive,
                                 "error": str(e)[:200]})
        elif p.path == "/restart":
            try:
                self._send(200, api.restart())
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)[:200]})
        elif p.path == "/session":
            try:
                self._send(200, api.session_info())
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)[:200]})
        elif p.path == "/jobs":
            import flow_store as _fs
            self._send(200, {"ok": True,
                             "jobs": _fs.jobs_list(int(qs.get("limit", ["30"])[0]))})
        elif p.path.startswith("/jobs/"):
            import flow_store as _fs
            jid = urllib.parse.unquote(p.path[len("/jobs/"):])
            j = _fs.job_get(jid)
            if not j:
                self._send(404, {"ok": False, "error": "unknown job"})
            else:
                self._send(200, {"ok": True, "job": j})
        elif p.path == "/logs":
            import flow_store as _fs
            self._send(200, {"ok": True, "logs": _fs.log_tail(
                int(qs.get("limit", ["100"])[0]), qs.get("day", [None])[0])})
        elif p.path == "/workers":
            import flow_store as _fs
            self._send(200, {"ok": True, "workers": _fs.workers_online()})
        elif p.path == "/worker/next":
            if API_KEYS and not self._auth_ok():
                self._send(401, {"ok": False, "error": "missing/invalid API key"})
                return
            import flow_store as _fs
            w = (qs.get("worker", ["worker"])[0] or "worker")[:40]
            job = _fs.claim_next_job(w)
            if not job:
                self._send(200, {"ok": True, "job": None})
            else:
                _fs.jlog("job_claimed", job=job["id"], worker=w)
                self._send(200, {"ok": True, "job": job})
        elif p.path == "/history":
            self._send(200, api.history())
        elif p.path == "/credits":
            self._send(200, api.credits())
        elif p.path == "/projects":
            self._send(200, api.projects())
        elif p.path == "/project":
            self._send(200, {"ok": True, "project": st.get("project_id", "")})
        elif p.path == "/project/new":
            nid = api.create_project()
            if nid:
                self._send(200, {"ok": True, "project": nid})
            else:
                self._send(500, {"ok": False, "error": "project create failed"})
        elif p.path.startswith("/media/"):
            mid = urllib.parse.unquote(p.path[len("/media/"):])
            fp = None
            for ext in ("jpg", "png", "webp"):
                cand = Path(__file__).parent / "outputs" / f"{mid}.{ext}"
                if cand.exists() and cand.stat().st_size > 10000:
                    fp = cand
                    break
            if fp is None:
                if ROLE == "all":
                    fp = api.media(mid)
                if not fp:
                    self._send(404, {"ok": False, "error": "download failed"})
                    return
            data = Path(fp).read_bytes()
            ctype = ("image/png" if fp.suffix == ".png"
                     else "image/webp" if fp.suffix == ".webp" else "image/jpeg")
            self._send(200, data, ctype=ctype,
                       headers={"Content-Disposition": f'attachment; filename="{Path(fp).name}"'})
        else:
            self._send(404, {"ok": False, "error": "not found",
                             "endpoints": ["/health", "/restart", "/session",
                                           "/generate", "/upscale", "/jobs",
                                           "/jobs/<id>", "/logs",
                                           "/history", "/media/<uuid>", "/credits",
                                           "/projects", "/project"]})

    def do_POST(self):
        if not self._gate(is_post=True, need_auth=True):
            return
        p = urllib.parse.urlparse(self.path)
        j = self._read_json()
        qs = urllib.parse.parse_qs(p.query)
        wait = (qs.get("wait", ["0"])[0] == "1")
        if p.path == "/generate":
            prompt = (j.get("prompt") or "").strip()
            if not prompt:
                self._send(400, {"ok": False, "error": "prompt required"})
                return
            print(f"[server] generate: {prompt[:60]!r}", flush=True)
            kw = dict(model=j.get("model") or None,
                      aspect=j.get("aspect") or None,
                      seed=j.get("seed"),
                      project_id=j.get("projectId") or None,
                      count=int(j.get("count") or 1))
            if wait and ROLE == "all":
                t0 = time.time()
                res = api.generate(prompt, **kw)
                res["elapsed_s"] = round(time.time() - t0, 1)
                self._send(200 if res.get("ok") else 500, res)
            else:
                jid = api.submit_generate(prompt, **kw)
                self._send(202, {"ok": True, "job": jid,
                                 "poll": f"/jobs/{jid}"})
        elif p.path == "/upscale":
            mid = (j.get("mediaId") or "").strip()
            if not mid:
                self._send(400, {"ok": False, "error": "mediaId required"})
                return
            print(f"[server] upscale {mid[:13]}… {j.get('resolution', '2K')}", flush=True)
            if wait and ROLE == "all":
                t0 = time.time()
                res = api.upscale(mid, j.get("resolution", "2K"),
                                  project_id=j.get("projectId") or None)
                res["elapsed_s"] = round(time.time() - t0, 1)
                self._send(200 if res.get("ok") else 500, res)
            else:
                jid = api.submit_upscale(mid, j.get("resolution", "2K"),
                                         project_id=j.get("projectId") or None)
                self._send(202, {"ok": True, "job": jid,
                                 "poll": f"/jobs/{jid}"})
        elif p.path == "/worker/heartbeat":
            import flow_store as _fs
            w = (j.get("worker") or "worker").strip()[:40]
            _fs.worker_heartbeat(w, j.get("info"))
            _fs.jlog("worker_heartbeat", worker=w)
            self._send(200, {"ok": True})
        elif p.path == "/worker/cookies":
            # home worker uploads fresh browser cookies so hosted reads
            # (history/credits) stay alive without a local login
            cookies = j.get("cookies") or []
            if not isinstance(cookies, list) or not cookies:
                self._send(400, {"ok": False, "error": "cookies required"})
                return
            try:
                clean = [{"name": c.get("name", ""), "value": c.get("value", ""),
                          "domain": c.get("domain", ""),
                          "path": c.get("path", "/")}
                         for c in cookies if c.get("name") and c.get("value")]
                fp = Path(__file__).parent / "sessions" / "flow.cookies.full.json"
                fp.parent.mkdir(exist_ok=True)
                fp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
                try:
                    api.http.reset()
                except Exception:
                    pass
                self._send(200, {"ok": True, "cookies": len(clean)})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)[:150]})
        elif p.path == "/worker/result":
            import base64 as _b64
            import flow_store as _fs
            jid = (j.get("job") or "").strip()
            if not jid or not _fs.job_get(jid):
                self._send(404, {"ok": False, "error": "unknown job"})
                return
            res = j.get("result") or {}
            # ingest worker-uploaded file bytes into local storage
            try:
                blobs = []
                if isinstance(res, dict):
                    if res.get("file_b64"):
                        blobs.append(("file", res.pop("file_b64")))
                    for it in (res.get("media") or []):
                        if isinstance(it, dict) and it.get("file_b64"):
                            blobs.append((it.get("uuid", "f"), it.pop("file_b64")))
                for name, b64 in blobs:
                    raw = _b64.b64decode(b64)
                    fname = (f"{res.get('uuid', 'file')}.jpg" if name == "file"
                             else f"{name}.jpg")
                    saved = _fs.save_blob(fname, raw)
                    if name == "file":
                        res["file"] = saved["path"] or res.get("file", "")
                        if saved["url"]:
                            res["url"] = saved["url"]
                    else:
                        for it in (res.get("media") or []):
                            if isinstance(it, dict) and it.get("uuid") == name:
                                it["file"] = saved["path"] or it.get("file", "")
                                if saved["url"]:
                                    it["url"] = saved["url"]
                if isinstance(res, dict) and "media" in res:
                    _fs.media_upsert(res["media"] if isinstance(res["media"], list)
                                     else [res["media"]])
            except Exception as e:
                print(f"[server] result ingest: {str(e)[:150]}", flush=True)
            ok = bool(j.get("ok", True))
            _fs.job_update(jid, status="done" if ok else "failed",
                           event="worker_finished",
                           result=res if ok else None,
                           error=None if ok else str(j.get("error", ""))[:300])
            _fs.jlog("worker_result", job=jid, ok=ok)
            self._send(200, {"ok": True})
        elif p.path == "/project":
            pid = (j.get("projectId") or "").strip()
            if not pid:
                self._send(400, {"ok": False, "error": "projectId required"})
                return
            st = load_state()
            st["project_id"] = pid
            save_state(st)
            self._send(200, {"ok": True, "project": pid})
        else:
            self._send(404, {"ok": False, "error": "not found"})


api = FlowAPI()
# all = local engine + API | api = queue only (hosted brain; worker elsewhere).
# Explicit ROLE wins. When unset, infer: no Chrome/profile here => this must be
# the hosted brain, so never try to boot an engine that can't exist.
ROLE = os.environ.get("ROLE", "").strip().lower()
if ROLE not in ("all", "api"):
    _has_engine = bool(flow_api.CHROME) and flow_api.PROFILE.exists()
    ROLE = "all" if _has_engine else "api"


class _Tee:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


def _setup_log():
    logf = open(Path(__file__).parent / "srv.live.log", "a", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, logf)
    sys.stderr = _Tee(sys.stderr, logf)


def main():
    _setup_log()
    host = "127.0.0.1"
    port = 8787
    args = sys.argv[1:]
    if "--host" in args:
        host = args[args.index("--host") + 1]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])

    print("=" * 60)
    print(f"  Google Flow HTTP API  (role={ROLE})")
    print("=" * 60)
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"[*] API listening on http://{host}:{port}", flush=True)
    if ROLE == "all":
        # Bind the port FIRST so Render's health check gets an instant 200,
        # then boot the engine in the background (chromium launch can take
        # 30-60s). /health reports engine state until it is ready.
        print("[*] starting invisible engine (background)...", flush=True)

        def _bg():
            try:
                r = api.start()
                if r.get("ok"):
                    print("[*] engine ready (off-screen Chrome)", flush=True)
                else:
                    print(f"[!] engine unavailable: {r.get('error', '')}",
                          flush=True)
            except Exception as e:  # noqa: BLE001
                api.local_run = False
                api.boot_error = str(e)[:300]
                print(f"[!] boot failed: {api.boot_error}", flush=True)

        threading.Thread(target=_bg, daemon=True).start()
    else:
        api.local_run = False
        try:
            api.start(boot_engine=False)
        except Exception as e:  # noqa: BLE001
            api.boot_error = str(e)[:300]
            print(f"[!] api boot failed: {api.boot_error}", flush=True)
        print("[*] api role: queue only (worker claims via /worker/next)",
              flush=True)

    print("    POST /generate  {'prompt': 'a red fox', 'count': 1} -> 202 job",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] shutting down...", flush=True)
    finally:
        server.server_close()
        try:
            api.close()
        except Exception:
            pass
        print("[*] bye")


if __name__ == "__main__":
    main()
