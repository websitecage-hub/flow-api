# -*- coding: utf-8 -*-
"""FlowAPI-compatible facade backed by the chrome-headless-shell engine.

Drop-in for flow_api.FlowAPI so flow_server.py runs unchanged with
FLOW_ENGINE=headless. Fits Render's free 512 MB tier (~120 MB engine).

Exposes: FlowAPI, load_state, save_state, norm_model, CHROME, PROFILE
(the names flow_server.py / gen.py import from flow_api).
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from flow_headless import (HeadlessEngine, detect_headless_shell, load_state,
                           rss_mb_of, PROFILE, ROOT, SITE_KEY)
import flow_store as _fs

ASPECTS = {"16:9": 3, "4:3": 5, "1:1": 1, "3:4": 4, "9:16": 2}
MODELS = ["NARWHAL", "HARBOR_SEAL", "GEM_PIX_2"]
MODEL_DISPLAY = {
    "NANO BANANA 2": "NARWHAL", "NANO BANANA 2 LITE": "HARBOR_SEAL",
    "NANO BANANA PRO": "GEM_PIX_2", "NARWHAL": "NARWHAL",
    "HARBOR_SEAL": "HARBOR_SEAL", "GEM_PIX_2": "GEM_PIX_2",
}
CHROME = detect_headless_shell()
STATE_FILE = ROOT / "sessions" / "flow_api_state.json"


def norm_model(name):
    if not name:
        return "NARWHAL"
    return MODEL_DISPLAY.get(str(name).strip().upper(), str(name).strip().upper())


def _load_dotenv():
    """Minimal .env loader (matches flow_api's)."""
    try:
        for ln in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except Exception:
        pass


class _NullHTTP:
    """Compat shim: flow_server references api.http.reset() in a couple of
    places. The headless engine has no separate HTTP session to reset."""
    def reset(self):
        return None


def save_state(st):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    import json
    STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")


class FlowAPI:
    """Serialized facade: all Playwright work on one worker thread."""

    def __init__(self):
        import queue
        self.engine = None
        self.lock = threading.Lock()
        self._q = queue.Queue()
        self._ready = threading.Event()
        self._active = 0
        self.local_run = True
        self.boot_error = ""
        self._thread = None
        self.http = _NullHTTP()

    # ── worker thread (Playwright sync API is thread-bound) ──────
    def _worker(self):
        while True:
            fn, ev, res = self._q.get()
            try:
                res["v"] = fn()
            except BaseException as e:  # noqa: BLE001
                res["err"] = e
            finally:
                ev.set()

    def run(self, fn, timeout=600):
        if not self._ready.is_set():
            raise RuntimeError("engine not started")
        with self.lock:
            self._active += 1
            try:
                ev = threading.Event()
                res = {}
                self._q.put((fn, ev, res))
                if not ev.wait(timeout):
                    raise TimeoutError("engine job timed out")
                if "err" in res:
                    raise res["err"]
                return res.get("v")
            finally:
                self._active -= 1

    def is_busy(self):
        try:
            return self._active > 1 or not self._q.empty()
        except Exception:
            return False

    def start(self, boot_engine=True):
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self._ready.set()
        if not boot_engine:
            return {"ok": True, "project": ""}

        def _boot():
            self.engine = HeadlessEngine()
            self.engine.start()
            return True

        try:
            self.run(_boot, timeout=180)
        except Exception as e:  # noqa: BLE001
            self.boot_error = str(e)[:300]
            print(f"[!] headless engine boot failed: {self.boot_error}", flush=True)
            return {"ok": False, "error": self.boot_error}
        # open the app once so the page + recaptcha are warm
        try:
            self.run(lambda: self.engine.goto_app("/"), timeout=120)
        except Exception as e:  # noqa: BLE001
            print(f"[*] warm goto: {str(e)[:120]}", flush=True)
        pid = load_state().get("project_id", "")
        return {"ok": True, "project": pid}

    def close(self):
        try:
            self.run(lambda: self.engine.close(), timeout=60)
        except Exception:
            pass

    # ── fast, non-blocking status (Render health checks) ─────────
    def _base_info(self):
        st = load_state()
        return {
            "ok": True,
            "engine_alive": self.engine is not None,
            "engine": "chrome-headless-shell",
            "rss_mb": round(rss_mb_of(str(PROFILE)), 1),
            "project": st.get("project_id", ""),
            "model": st.get("model", "NARWHAL"),
            "busy": self.is_busy(), "local_run": self.local_run,
            "cookies_ready": (ROOT / "sessions" / "flow.cookies.full.json").exists(),
            "boot_error": self.boot_error,
        }

    def status(self):
        return self._base_info()

    def session_info(self):
        def job():
            out = self._base_info()
            sess = False
            try:
                pid = out.get("project") or ""
                if pid:
                    d = self.engine.history(pid)
                    sess = d is not None
            except Exception:
                sess = False
            out["session_ok"] = sess
            if not sess:
                out["login_steps"] = [
                    "1. Log in once on the profile this container uses "
                    "(FLOW_PROFILE) and copy it in, OR set FLOW_COOKIES_JSON.",
                    "2. Then GET /session should report session_ok=true.",
                    "Free tier has no persistent disk: re-seed the profile "
                    "after each deploy (PROFILE_TAR_URL).",
                ]
            return out
        return self.run(job, timeout=120)

    def restart(self):
        def job():
            self.engine.close()
            self.engine = HeadlessEngine()
            self.engine.start()
            self.engine.goto_app("/")
            return {"ok": True}
        return self.run(job, timeout=300)

    # ── jobs ─────────────────────────────────────────────────────
    def _submit_job(self, kind, params, fn, timeout=900):
        jid = _fs.new_job(kind, params)
        _fs.jlog("job_queued", job=jid, kind=kind)
        if not self.local_run:
            return jid

        def _runner():
            _fs.job_update(jid, status="running", event="started")
            try:
                res = self.run(fn, timeout=timeout)
                ok = not (isinstance(res, dict) and res.get("ok") is False)
                if isinstance(res, dict) and res.get("media"):
                    try:
                        _fs.media_upsert(res["media"])
                    except Exception:
                        pass
                _fs.job_update(jid, status="done" if ok else "failed",
                               event="finished",
                               result=res if ok else None,
                               error=None if ok else (
                                   res.get("error") if isinstance(res, dict)
                                   else "failed"))
                _fs.jlog("job_done", job=jid, ok=ok)
            except Exception as e:  # noqa: BLE001
                _fs.job_update(jid, status="failed", event="crashed",
                               error=str(e)[:300])
                _fs.jlog("job_failed", job=jid, error=str(e)[:200])

        threading.Thread(target=_runner, daemon=True).start()
        return jid

    def submit_generate(self, prompt, model=None, aspect=None, seed=None,
                        project_id=None, count=1):
        def job():
            items = self.engine.generate(prompt, model=model, aspect=aspect,
                                         count=count, project_id=project_id,
                                         seed=seed)
            if not items:
                return {"ok": False, "error": "generation failed"}
            return {"ok": True, "media": items, **items[0]}
        return self._submit_job("generate", {
            "prompt": prompt, "model": model, "aspect": aspect,
            "seed": seed, "count": count, "project_id": project_id}, job)

    def submit_upscale(self, media_uuid, resolution="2K", project_id=None):
        def job():
            out = self.engine.upscale(media_uuid, resolution, project_id)
            if not out:
                return {"ok": False, "error": "upscale failed"}
            return {"ok": True, "source": media_uuid, "file": str(out)}
        return self._submit_job("upscale", {
            "media_uuid": media_uuid, "resolution": resolution,
            "project_id": project_id}, job)

    def generate(self, prompt, model=None, aspect=None, seed=None,
                 project_id=None, count=1):
        def job():
            items = self.engine.generate(prompt, model=model, aspect=aspect,
                                         count=count, project_id=project_id,
                                         seed=seed)
            if not items:
                return {"ok": False, "error": "generation failed"}
            return {"ok": True, "media": items, **items[0]}
        return self.run(job, timeout=600)

    def upscale(self, media_uuid, resolution="2K", project_id=None):
        def job():
            out = self.engine.upscale(media_uuid, resolution, project_id)
            return ({"ok": True, "source": media_uuid, "file": str(out)}
                    if out else {"ok": False, "error": "upscale failed"})
        return self.run(job, timeout=600)

    def history(self, project_id=None):
        def job():
            pid = project_id or load_state().get("project_id", "")
            if not pid:
                return {"ok": False, "error": "no project set"}
            media = self.engine.history(pid)
            if media is None:
                cached = _fs.media_list(limit=100)
                if cached:
                    return {"ok": True, "project": pid, "media": cached,
                            "stale": True}
                return {"ok": False, "error": "history failed (login?)"}
            return {"ok": True, "project": pid, "media": media}
        return self.run(job)

    def credits(self):
        def job():
            c = self.engine.credits()
            return ({"ok": True, **c} if c
                    else {"ok": False, "error": "credits failed (login?)"})
        return self.run(job)

    def projects(self):
        return {"ok": True, "projects": []}

    def create_project(self):
        return None

    def media(self, media_uuid):
        return None