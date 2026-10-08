# -*- coding: utf-8 -*-
"""FlowAPI-compatible facade backed by the chrome-headless-shell engine.

Drop-in for flow_api.FlowAPI so flow_server.py runs unchanged with
FLOW_ENGINE=headless. Fits Render's free 512 MB tier (~120 MB engine).

Exposes: FlowAPI, load_state, save_state, norm_model, CHROME, PROFILE
(the names flow_server.py / gen.py import from flow_api).
"""
from __future__ import annotations

import glob
import os
import queue
import threading
import time
from pathlib import Path

from flow_headless import (HeadlessEngine, detect_headless_shell, load_state,
                           rss_mb_of, PROFILE, ROOT, SITE_KEY)
import flow_store as _fs

ASPECTS = {"16:9": 3, "4:3": 5, "1:1": 1, "3:4": 4, "9:16": 2}
MODELS = ["NARWHAL", "HARBOR_SEAL", "GEM_PIX_2"]  # keep list stable; BELUGA valid via norm_model
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
        self._q = queue.Queue()
        self._ready = threading.Event()
        self._counter_lock = threading.Lock()
        self._active = 0
        self.local_run = True
        self.boot_error = ""
        self._thread = None
        self.http = _NullHTTP()
        self.last_engine_ok = 0.0        # epoch s of last successful engine op
        self._rebuild_inflight = False   # single-flight guard for rebuilds
        self._rebuild_lock = threading.Lock()

    # ── worker thread (Playwright sync API is thread-bound) ──────
    def _worker(self, my_q):
        while True:
            item = my_q.get()
            if item is None:            # poison pill: worker is being replaced
                return
            fn, ev, res = item
            try:
                res["v"] = fn()
            except BaseException as e:  # noqa: BLE001
                res["err"] = e
            finally:
                ev.set()

    def _start_worker(self):
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._worker,
                                        args=(self._q,), daemon=True)
        self._thread.start()

    def _kill_browser_procs(self):
        """SIGKILL every chrome-headless-shell tied to our profile dir.
        Frees memory immediately and un-wedges any call stuck in Playwright.
        Returns number killed."""
        import signal
        killed = 0
        targets = []
        for p in glob.glob("/proc/[0-9]*"):
            try:
                cmd = open(p + "/cmdline", "rb").read().replace(
                    b"\0", b" ").decode("utf-8", "replace")
                if (("chrome-headless-shell" in cmd or "headless_shell" in cmd)
                        and str(PROFILE) in cmd):
                    targets.append(int(p.split("/")[-1]))
            except Exception:
                pass
        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except Exception:
                pass
        if killed:
            print(f"[*] SIGKILLed {killed} stray browser process(es)", flush=True)
        return killed

    def run(self, fn, timeout=600, _boot=False):
        """Enqueue fn on the worker and wait up to `timeout`. NEVER holds a
        lock while waiting — one slow call must not block every other call.
        On timeout the worker is considered wedged: hard-rebuild the engine
        (unless this call IS the boot) and raise."""
        if not self._ready.is_set():
            raise RuntimeError("engine not started")
        if self._thread is None or not self._thread.is_alive():
            self._start_worker()
        ev = threading.Event()
        res = {}
        self._q.put((fn, ev, res))
        with self._counter_lock:
            self._active += 1
        try:
            if not ev.wait(timeout):
                if not _boot and not self._rebuild_inflight:
                    try:
                        self.hard_rebuild()
                    except Exception as e:
                        print(f"[!] auto rebuild failed: {str(e)[:120]}", flush=True)
                raise TimeoutError(f"engine job timed out after {timeout}s")
            if "err" in res:
                raise res["err"]
            self.last_engine_ok = time.time()
            return res.get("v")
        finally:
            with self._counter_lock:
                self._active -= 1

    def is_busy(self):
        try:
            return self._active > 1 or not self._q.empty()
        except Exception:
            return False

    def hard_rebuild(self, boot=True):
        """Supervisor recovery: kill the browser tree, drop the wedged worker,
        boot a fresh engine on a fresh worker. Single-flight guarded."""
        with self._rebuild_lock:
            if self._rebuild_inflight:
                return {"ok": False, "error": "rebuild already in progress"}
            self._rebuild_inflight = True
            try:
                print("[*] hard rebuild: killing browser + resetting engine", flush=True)
                self._kill_browser_procs()
                self.engine = None
                self._thread = None          # abandon wedged worker
                self._q = queue.Queue()
                self.boot_error = ""
                if not boot:
                    return {"ok": True}
                return self._boot_engine()
            finally:
                self._rebuild_inflight = False

    def _boot_engine(self):
        def _boot():
            eng = HeadlessEngine()
            eng.start()
            return eng
        try:
            eng = self.run(_boot, timeout=200, _boot=True)
            self.engine = eng
            try:
                # warm the app + recaptcha
                self.run(lambda: eng.goto_app("/"), timeout=90, _boot=True)
            except Exception as e:
                print(f"[*] warm goto: {str(e)[:120]}", flush=True)
            self.last_engine_ok = time.time()
            return {"ok": True, "engine": "chrome-headless-shell"}
        except Exception as e:
            self.boot_error = str(e)[:300]
            print(f"[!] engine boot failed: {self.boot_error}", flush=True)
            return {"ok": False, "error": self.boot_error}

    def start(self, boot_engine=True):
        self._start_worker()
        self._ready.set()
        if not boot_engine:
            return {"ok": True, "project": ""}
        return self._boot_engine()

    def close(self):
        try:
            self._kill_browser_procs()
        except Exception:
            pass
        try:
            if self._q:
                self._q.put(None)
        except Exception:
            pass
        self.engine = None

    def restart(self):
        """Public restart for /restart + watchdog. Synchronous, bounded."""
        return self.hard_rebuild(boot=True)

    # ── fast, non-blocking status (Render health checks) ─────────
    def _base_info(self):
        st = load_state()
        stale = (time.time() - self.last_engine_ok) > 180 if self.last_engine_ok else True
        rss = 0.0
        try:
            rss = round(rss_mb_of(str(PROFILE)), 1)
        except Exception:
            pass
        return {
            "ok": True,
            "engine_alive": self.engine is not None,
            "engine_responsive": not stale,
            "engine": "chrome-headless-shell",
            "rss_mb": rss,
            "project": st.get("project_id", ""),
            "model": st.get("model", "BELUGA"),
            "busy": self.is_busy(), "local_run": self.local_run,
            "cookies_ready": (ROOT / "sessions" / "flow.cookies.full.json").exists()
                             or bool(os.environ.get("FLOW_COOKIES_JSON", "").strip()),
            "boot_error": self.boot_error,
            "last_engine_ok_age_s": round(time.time() - self.last_engine_ok, 1)
                                    if self.last_engine_ok else -1,
        }

    def status(self):
        return self._base_info()

    def session_info(self):
        def job():
            out = self._base_info()
            sess = False
            try:
                pid = out.get("project") or ""
                if pid and self.engine is not None:
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
        try:
            return self.run(job, timeout=90)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "session_ok": False}

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
        try:
            return self.run(job, timeout=90)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def credits(self):
        def job():
            c = self.engine.credits()
            return ({"ok": True, **c} if c
                    else {"ok": False, "error": "credits failed (login?)"})
        try:
            return self.run(job, timeout=60)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def projects(self):
        return {"ok": True, "projects": []}

    def create_project(self):
        return None

    def media(self, media_uuid):
        return None