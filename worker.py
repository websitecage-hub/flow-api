#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flow engine worker — runs where Chrome + the logged-in profile live.

Polls the API (local or hosted) for queued jobs, executes them on the
off-screen engine, uploads results (incl. image bytes) back.

  python worker.py --api http://127.0.0.1:8787 --key SECRET --name home-pc
  ROLE=worker python worker.py            (same via env)

Env: API_BASE, API_KEY, WORKER_NAME. Google throttle + guardian active
(FLOW_MIN_SUBMIT_INTERVAL etc.), so the account is never hammered.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from flow_api import FlowEngine, PureHTTP, load_state  # noqa: E402

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def _env(name, default=""):
    return os.environ.get(name, default)


class Remote:
    def __init__(self, base, key):
        self.base = base.rstrip("/")
        self.key = key

    def _req(self, method, path, obj=None, timeout=60):
        data = json.dumps(obj or {}).encode() if obj is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if self.key:
            req.add_header("Authorization", f"Bearer {self.key}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def claim(self, worker):
        try:
            r = self._req("GET", f"/worker/next?worker={urllib.parse.quote(worker)}")
            return r.get("job")
        except Exception:
            return None

    def heartbeat(self, worker, info=None):
        try:
            self._req("POST", "/worker/heartbeat",
                      {"worker": worker, "info": info or {}})
        except Exception:
            pass

    def push_cookies(self, cookies):
        try:
            return self._req("POST", "/worker/cookies", {"cookies": cookies})
        except Exception as e:
            print(f"[!] cookie sync failed: {str(e)[:120]}", flush=True)
            return None

    def result(self, job, ok, result=None, error=""):
        try:
            self._req("POST", "/worker/result",
                      {"job": job, "ok": ok, "result": result or {},
                       "error": error}, timeout=120)
        except Exception as e:
            print(f"[!] result post failed: {str(e)[:150]}", flush=True)


def with_b64_files(result):
    """Attach local file bytes (base64) so the API can store/serve them."""
    try:
        paths = []
        if isinstance(result, dict):
            if result.get("file"):
                paths.append(("file", result["file"]))
            for it in (result.get("media") or []):
                if isinstance(it, dict) and it.get("file"):
                    paths.append((it.get("uuid", "f"), it["file"]))
        for name, fp in paths:
            try:
                raw = Path(fp).read_bytes()
                b64 = base64.b64encode(raw).decode()
                if name == "file":
                    result["file_b64"] = b64
                else:
                    for it in (result.get("media") or []):
                        if isinstance(it, dict) and it.get("uuid") == name:
                            it["file_b64"] = b64
            except Exception:
                pass
    except Exception:
        pass
    return result


def main():
    ap = argparse.ArgumentParser(description="Flow engine worker")
    ap.add_argument("--api", default=_env("API_BASE", "http://127.0.0.1:8787"))
    ap.add_argument("--key", default=_env("API_KEY", ""))
    ap.add_argument("--name", default=_env("WORKER_NAME", "home-pc"))
    ap.add_argument("--poll", type=int, default=10)
    a = ap.parse_args()

    print(f"[*] worker '{a.name}' -> {a.api}", flush=True)
    remote = Remote(a.api, a.key)
    eng = FlowEngine()
    try:
        eng.start()
    except SystemExit as e:
        return e.code or 1
    http = PureHTTP()
    idle = 0
    last_cookie_sync = 0.0
    try:
        while True:
            remote.heartbeat(a.name, {"project": load_state().get("project_id", "")})
            # keep hosted reads alive: push fresh browser cookies every 15 min
            try:
                if time.time() - last_cookie_sync > 900 and eng.browser:
                    jar = []
                    for ctx in eng.browser.contexts:
                        for c in ctx.cookies():
                            jar.append({"name": c["name"], "value": c["value"],
                                        "domain": c["domain"],
                                        "path": c.get("path", "/")})
                    if jar:
                        r = remote.push_cookies(jar)
                        if r and r.get("ok"):
                            last_cookie_sync = time.time()
                            print(f"[*] synced {r.get('cookies')} cookies",
                                  flush=True)
            except Exception as e:
                print(f"[!] cookie sync: {str(e)[:120]}", flush=True)
            job = remote.claim(a.name)
            if not job:
                idle += 1
                time.sleep(a.poll)
                continue
            idle = 0
            jid, kind, params = job["id"], job["kind"], job.get("params", {})
            print(f"[*] claimed {jid} ({kind})", flush=True)
            try:
                if kind == "generate":
                    from flow_api import norm_model
                    eng.set_gen_overrides(
                        imageModelName=norm_model(params.get("model") or "NARWHAL"))
                    if params.get("aspect"):
                        eng.set_gen_overrides(imageAspectRatio=params["aspect"])
                    else:
                        eng.set_gen_overrides(imageAspectRatio=None)
                    if params.get("seed") is not None:
                        eng.set_gen_overrides(seed=int(params["seed"]))
                    else:
                        eng.set_gen_overrides(seed=None)
                    items = eng.generate(params.get("prompt", ""),
                                         model=params.get("model") or "NARWHAL",
                                         aspect=params.get("aspect"),
                                         count=int(params.get("count") or 1),
                                         project_id=params.get("project_id"))
                    if not items:
                        remote.result(jid, False, {}, "generation failed")
                    else:
                        first = items[0]
                        remote.result(jid, True, with_b64_files({
                            "ok": True, "uuid": first.get("uuid"),
                            "seed": first.get("seed"),
                            "prompt": first.get("prompt"),
                            "width": first.get("width"),
                            "height": first.get("height"),
                            "file": first.get("file"),
                            "files": [it.get("file") for it in items],
                            "media": items}))
                elif kind == "upscale":
                    out = eng.upscale(params.get("media_uuid", ""),
                                      params.get("resolution", "2K"),
                                      params.get("project_id"))
                    if not out:
                        remote.result(jid, False, {}, "upscale failed")
                    else:
                        remote.result(jid, True, with_b64_files({
                            "ok": True, "source": params.get("media_uuid", ""),
                            "file": str(out)}))
                else:
                    remote.result(jid, False, {}, f"unknown kind {kind}")
            except Exception as e:  # noqa: BLE001
                print(f"[!] job {jid} crashed: {str(e)[:200]}", flush=True)
                try:
                    remote.result(jid, False, {}, f"worker crash: {str(e)[:200]}")
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[*] worker stopping", flush=True)
    finally:
        try:
            eng.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
