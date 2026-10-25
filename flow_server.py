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
from flow_api import load_state, save_state

# Engine selection: FLOW_ENGINE=headless runs the chrome-headless-shell engine
# (single process, ~120 MB) which fits Render's free 512 MB tier. Default
# keeps the original full-Chrome engine.
# PRODUCTION (2026-10-08): default to headless when FLOW_ENGINE is unset. The
# old full-Chrome engine boots at ~1.2 GB and OOM-kills free-tier instances on
# every wake ("web service exceeded its memory limit") — the blackout cause.
# Only an explicit FLOW_ENGINE=chrome/full opts into the heavy engine now.
ENGINE_RAW = os.environ.get("FLOW_ENGINE", "").strip().lower()
if ENGINE_RAW in ("", "headless", "light", "shell"):
    import flow_api_headless as flow_api  # noqa: F811
    from flow_api_headless import FlowAPI, load_state, save_state  # noqa: F401
else:
    from flow_api import FlowAPI  # noqa: F401

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
# /upload ceiling, in DECODED megabytes. The image is base64'd and then
# urlencoded on the wire, so peak memory is several times the file size — and
# this container is 512MB shared with the browser. 4MB of image = ~5.4MB b64
# = ~9MB urlencoded, which is comfortable; anything larger risks the OOM
# killer, which would take the whole service down for every caller.
MAX_UPLOAD_MB = float(os.environ.get("FLOW_MAX_UPLOAD_MB", "4"))
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
        """Key required when API_KEYS is configured.

        Accepts the Bearer header for programs *and* ?key=<secret> so a plain
        browser link or <img src="..."> works — an img tag cannot set headers,
        which is exactly why the media URLs looked broken when opened directly.
        """
        if not API_KEYS:
            return True
        auth = self.headers.get("Authorization", "") or ""
        if auth.startswith("Bearer ") and auth[7:].strip() in API_KEYS:
            return True
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            for field in ("key", "token", "api_key"):
                v = (q.get(field) or [""])[0].strip()
                if v and v in API_KEYS:
                    return True
        except Exception:
            pass
        return False

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
        head_only = getattr(self, "_head", False)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # Content-Length is the length the BODY WOULD have, even for HEAD
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if head_only:
            return
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_raw(self):
        """The raw POST body, whatever it is. /upload needs this: a caller may
        send the image bytes directly (curl --data-binary) instead of JSON."""
        ln = int(self.headers.get("Content-Length") or 0)
        if ln <= 0:
            return b""
        return self.rfile.read(ln)

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

    def do_HEAD(self):
        """UptimeRobot (and other monitors) may be configured to probe with
        HEAD. Without this, BaseHTTPRequestHandler answers 501 and the monitor
        reports the service DOWN while it is perfectly healthy."""
        self._head = True
        self.do_GET()

    def do_GET(self):
        if not self._gate(is_post=False, need_auth=False):
            return
        p = urllib.parse.urlparse(self.path)
        # tolerate a trailing slash: "/health/" is a common monitor typo and
        # used to 404, which reads as DOWN on the dashboard
        if p.path != "/" and p.path.endswith("/"):
            p = p._replace(path=p.path.rstrip("/"))
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
        elif p.path in ("/", "/ui", "/ui/", "/ui/index.html", "/index.html"):
            # Live test bench — public (rate-limited, never auth-gated) so you
            # can try generation from your phone anywhere. Serves the static
            # page; all API calls it makes go through the same endpoints.
            try:
                ui = Path(__file__).parent / "ui" / "index.html"
                if not ui.exists():
                    self._send(404, {"ok": False, "error": "ui not found"})
                    return
                data = ui.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if getattr(self, "_head", False):
                    return
                try:
                    self.wfile.write(data)
                except Exception:
                    pass
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)[:200]})
        elif p.path == "/session":
            try:
                self._send(200, api.session_info())
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)[:200]})
        elif p.path == "/diag":
            # auth-gated: runs a real engine self-test (boot/load/mint/credits)
            if API_KEYS and not self._auth_ok():
                self._send(401, {"ok": False, "error": "missing/invalid API key"})
                return
            fn = getattr(api, "diag", None)
            if fn is None:
                self._send(501, {"ok": False, "error": "diag unavailable (no engine here)"})
                return
            try:
                self._send(200, fn())
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
        elif p.path == "/models":
            # full catalogue: every model, aspect, upscale target, endpoint.
            # Static tables -> instant, and safe to call while a job runs.
            fn = getattr(api, "models", None)
            self._send(200, fn() if fn else {"ok": False,
                                             "error": "models unavailable in this role"})
        elif p.path == "/whoami":
            # EXPERIMENTAL / OFF BY DEFAULT. The o30O0e payload below is from
            # FLOW-INTERNALS.md but is unvalidated: calling it left the page
            # wedged (Runtime.evaluate timeout), which would stall the same
            # worker generations use. Gate it until the wire shape is proven.
            if os.environ.get("FLOW_EXPERIMENTAL", "") != "1":
                self._send(200, {"ok": False, "experimental": True,
                                 "error": "disabled — set FLOW_EXPERIMENTAL=1 "
                                          "to enable (unvalidated RPC; can "
                                          "wedge the page for ~60s)"})
                return
            fn = getattr(api, "whoami", None)
            self._send(200, fn() if fn else {"ok": False,
                                             "error": "whoami unavailable in this role"})
        elif p.path == "/projects":
            self._send(200, api.projects())
        elif p.path == "/project":
            self._send(200, {"ok": True, "project": st.get("project_id", "")})
        elif p.path == "/project/new":
            res = api.create_project()
            if isinstance(res, dict):
                self._send(200 if res.get("ok") else 500, res)
            elif res:
                self._send(200, {"ok": True, "project": res})
            else:
                self._send(500, {"ok": False, "error": "project create failed"})
        elif p.path == "/media":
            # Recent media, newest first. Without this the ONLY way an image was
            # ever reachable was the tab that submitted it — reload the page (the
            # natural thing to do when a job looks slow) and a finished result
            # became invisible forever.
            import flow_store as _fs
            self._send(200, {"ok": True, "media": _fs.media_list(
                int(qs.get("limit", ["48"])[0]))})
        elif p.path.startswith("/media/"):
            mid = urllib.parse.unquote(p.path[len("/media/"):])
            if qs.get("info", [""])[0] in ("1", "true", "yes"):
                # metadata only (signed URLs, seed, prompt) instead of bytes.
                # EXPERIMENTAL/OFF BY DEFAULT: the as29s payload is unvalidated
                # and returned nothing while wedging the page — see /whoami.
                if os.environ.get("FLOW_EXPERIMENTAL", "") != "1":
                    self._send(200, {"ok": False, "experimental": True,
                                     "error": "disabled — set FLOW_EXPERIMENTAL=1 "
                                              "to enable (unvalidated RPC)"})
                    return
                fn = getattr(api, "media_info", None)
                self._send(200, fn(mid) if fn else
                           {"ok": False, "error": "media_info unavailable"})
                return
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
                             "endpoints": ["/health", "/models", "/whoami",
                                           "/restart", "/session", "/diag",
                                           "/generate", "/upscale", "/upload",
                                           "/jobs",
                                           "/jobs/<id>", "/logs",
                                           "/history", "/media/<uuid>",
                                           "/media/<uuid>?info=1", "/credits",
                                           "/projects", "/project", "/ui"]})

    def do_POST(self):
        # Drain the request body FIRST — if we 401/429 before reading it, the
        # leftover bytes corrupt the next request on a keep-alive connection
        # (client sees "Bad request syntax '<json body>GET /... HTTP/1.1'").
        # Read it RAW so /upload can also accept the image bytes directly.
        raw_body = b""
        try:
            raw_body = self._read_raw()
        except Exception:
            raw_body = b""
        j = {}
        if raw_body:
            try:
                j = json.loads(raw_body.decode("utf-8"))
            except Exception:
                j = {}
        if not self._gate(is_post=True, need_auth=True):
            return
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        wait = (qs.get("wait", ["0"])[0] == "1")
        if p.path == "/generate":
            # ONE-GENERATION-AT-A-TIME GUARD: overlapping generates stack
            # page loads inside the single chrome (each ~110MB of Angular
            # hydration) and snowball the container past 512MB. Return 429
            # with Retry-After so clients poll/back off instead.
            try:
                if api.is_busy():
                    self._send(429, {"ok": False,
                                     "error": "a generation is already running",
                                     "retry_after_s": 30},
                               headers={"Retry-After": "30"})
                    return
            except Exception:
                pass
            prompt = (j.get("prompt") or "").strip()
            if not prompt:
                self._send(400, {"ok": False, "error": "prompt required"})
                return
            # VALIDATE model/aspect instead of silently defaulting: a typo used
            # to produce a BELUGA image and look like the request was honoured.
            try:
                from flow_headless import MODEL_WIRE, MODEL_DISPLAY, ASPECT_WIRE
                want_m = (j.get("model") or "").strip().upper()
                if want_m:
                    ok_m = (want_m in MODEL_WIRE) or (want_m in MODEL_DISPLAY)
                    if not ok_m:
                        self._send(400, {
                            "ok": False,
                            "error": f"unknown model {j.get('model')!r}",
                            "models": sorted(MODEL_WIRE),
                            "aliases": sorted(MODEL_DISPLAY),
                            "hint": "GET /models for the full catalogue",
                        })
                        return
                want_a = (j.get("aspect") or "").strip()
                if want_a and want_a not in ASPECT_WIRE:
                    self._send(400, {
                        "ok": False,
                        "error": f"unknown aspect {want_a!r}",
                        "aspects": sorted(ASPECT_WIRE),
                        "hint": "GET /models for the full catalogue",
                    })
                    return
            except ImportError:
                pass
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
        elif p.path == "/upload":
            # Upload a reference image so it appears in the project's Uploads
            # as a prompt ingredient. Mapped from live traffic: RPC `maseQ`,
            # with the bytes INLINE as base64 in an ordinary batchexecute POST
            # (no multipart endpoint exists in the Flow app). Issued in-page
            # through JS_RPC like every other call here.
            #
            # Two accepted forms:
            #   1. JSON  {"image_base64": "...", "filename": "x.jpg",
            #             "mime": "image/jpeg", "projectId": "<uuid>"}
            #      ("image" and "data" are accepted as aliases)
            #   2. the raw image bytes as the body with an image/* content-type
            #      (curl --data-binary @x.jpg -H 'Content-Type: image/jpeg')
            ctype = (self.headers.get("Content-Type") or "").lower()
            data = ""
            filename = ""
            mime = "image/jpeg"
            if isinstance(j, dict) and (j.get("image_base64") or j.get("image")
                                        or j.get("data")):
                data = (j.get("image_base64") or j.get("image")
                        or j.get("data") or "")
                filename = (j.get("filename") or "").strip()
                mime = ((j.get("mime") or j.get("content_type") or "").strip()
                        or mime)
            elif raw_body and not ctype.startswith("application/json"):
                import base64 as _b64
                data = _b64.b64encode(raw_body).decode("ascii")
                mime = ctype.split(";")[0].strip() or mime
                filename = (qs.get("filename", [""])[0] or "").strip()
            if not data:
                self._send(400, {
                    "ok": False,
                    "error": "image required",
                    "hint": ('send JSON {"image_base64": "<base64>"} or the raw '
                             'image bytes with an image/* Content-Type'),
                    "max_mb": MAX_UPLOAD_MB,
                })
                return
            # base64 of the body must fit the memory budget: this is a 512MB
            # container shared with the browser, and the payload is copied a
            # few times on the way through (json -> b64 -> urlencoded form).
            approx_mb = (len(str(data)) * 3 // 4) / 1048576.0
            if approx_mb > MAX_UPLOAD_MB:
                self._send(413, {
                    "ok": False,
                    "error": f"image too large ({approx_mb:.1f} MB decoded)",
                    "max_mb": MAX_UPLOAD_MB,
                })
                return
            fn = getattr(api, "upload", None)
            if fn is None:
                self._send(501, {"ok": False,
                                 "error": "upload unavailable in this role"})
                return
            # Same one-job-at-a-time reasoning as /generate: the upload shares
            # the single worker (and the single page), so queueing behind a
            # 10-minute generation would just time the caller out.
            try:
                if api.is_busy():
                    self._send(429, {"ok": False,
                                     "error": "a job is already running",
                                     "retry_after_s": 30},
                               headers={"Retry-After": "30"})
                    return
            except Exception:
                pass
            print(f"[server] upload: {len(str(data))} b64 chars, "
                  f"mime={mime}, name={filename or '(auto)'}", flush=True)
            res = fn(data, mime=mime, filename=filename,
                     project_id=((j.get("projectId") or j.get("project_id")
                                  or None) if isinstance(j, dict) else None))
            self._send(200 if res.get("ok") else 500, res)
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
                # also write next to the server (some paths look there)
                try:
                    Path(__file__).parent.joinpath("flow.cookies.full.json").write_text(
                        json.dumps(clean, indent=2), encoding="utf-8")
                except Exception:
                    pass
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
        elif p.path == "/cookies":
            # Push a fresh cookie export to a running service (phone-friendly).
            fn = getattr(api, "reseed_cookies", None)
            if fn is None:
                self._send(501, {"ok": False, "error": "not available"})
                return
            body = j.get("cookies") if isinstance(j, dict) else None
            if j and j.get("cookies") is None and isinstance(j, list):
                body = j
            try:
                self._send(200, fn(body if body is not None else j))
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)[:200]})
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
# PRODUCTION SELF-HEAL (2026-10-08): a self-contained image (headless engine
# selected AND browser present) MUST boot its own engine — a stale ROLE=api env
# var left over from the old split-brain deploy must not disable generation.
# Cookies only gate the SESSION (login), not the engine boot.
if ROLE == "api":
    _engine_available = bool(getattr(flow_api, "CHROME", "") or "")
    if _engine_available:
        print("[*] engine binary present -> forcing ROLE=all (self-contained)", flush=True)
        ROLE = "all"
    else:
        print("[*] no engine binary -> staying ROLE=api (queue only)", flush=True)


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


def _watchdog(interval=45):
    """Watch the engine: if it died, boot failed, OR went unresponsive (stale
    heartbeat), hard-rebuild it. This is what keeps the API from going black
    when the browser/reCAPTCHA wedges. Uses the facade's supervisor restart
    (SIGKILL browser tree + fresh worker), never a lock-holding call."""
    import time as _t
    while True:
        _t.sleep(interval)
        try:
            alive = getattr(api, "engine", None) is not None
            boot_err = getattr(api, "boot_error", "") or ""
            last_ok = getattr(api, "last_engine_ok", 0.0) or 0.0
            stale = (time.time() - last_ok) > 180 if last_ok else True
            if (not alive) or boot_err or stale:
                print(f"[watchdog] engine dead/alive={alive} boot_err={boot_err!r} stale={stale} — rebuilding",
                      flush=True)
                try:
                    r = api.restart()
                    api.boot_error = ""
                    print(f"[watchdog] engine rebuilt: {r}", flush=True)
                except Exception as e:
                    print(f"[watchdog] rebuild failed: {str(e)[:200]}", flush=True)
        except Exception as e:
            print(f"[watchdog] error: {str(e)[:150]}", flush=True)


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
                    api.boot_error = r.get("error", "")
            except Exception as e:  # noqa: BLE001
                api.local_run = False
                api.boot_error = str(e)[:300]
                print(f"[!] boot failed: {api.boot_error}", flush=True)

        threading.Thread(target=_bg, daemon=True).start()
        # NOTE: the engine facade now owns supervision (liveness probe + session
        # self-heal) via FlowAPI._supervise(), started in api.start(). No second
        # watchdog here — two rebuild loops would thrash the engine.
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
