# -*- coding: utf-8 -*-
"""FlowAPI-compatible facade backed by the chrome-headless-shell engine.

Drop-in for flow_api.FlowAPI so flow_server.py runs unchanged with
FLOW_ENGINE=headless. Fits Render's free 512 MB tier (~120 MB engine).

Exposes: FlowAPI, load_state, save_state, norm_model, CHROME, PROFILE
(the names flow_server.py / gen.py import from flow_api).
"""
from __future__ import annotations

import glob
import json
import os
import queue
import threading
import time
from pathlib import Path

from flow_headless import (HeadlessEngine, detect_headless_shell, load_state,
                           rss_mb_of, total_tree_rss_mb, read_rss_mb,
                           proc_breakdown,
                           MEM_GUARD_MB, PROFILE, ROOT, SITE_KEY)
import flow_store as _fs
import flow_auth as _auth

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


# ── pure-Python auth layer (flow_auth): jar + at token + rotation ──
class _AuthState:
    """Holds the cookie jar + batchexecute tokens; owns the rotation loop.
    Boot order (handoff §4): KV -> FLOW_COOKIES_JSON env -> master-token mint."""

    def __init__(self):
        self.jar = {}                 # name -> value (the live truth)
        self.bl = ""
        self.fsid = ""
        self.at = ""
        self.at_ts = 0.0
        self.last_rotate = 0.0
        self.last_rotate_status = 0
        self.last_rotate_ok = None
        self.minted = False
        self.source = "none"          # kv | env | file | master | browser
        self._lock = threading.Lock()
        self.loop = None

    # -- jar management -------------------------------------------------
    def load(self):
        # Boot priority (handoff: "always mint from the master token"):
        #   KV (survives deploys) -> MASTER TOKEN MINT (unbound jar) ->
        #   FLOW_COOKIES_JSON env -> sessions file (last resorts; a phone/
        #   desktop export is a BOUND session and RotateCookies rejects it,
        #   so it only matters when no master token is configured).
        kv = _auth.kv_get()
        if kv:
            self.jar, self.source = kv, "kv"
        master = os.environ.get("FLOW_MASTER_TOKEN", "").strip()
        email = (os.environ.get("FLOW_ACCOUNT_EMAIL", "")
                 or os.environ.get("FLOW_EMAIL", "")).strip()
        # Previously: "skip the mint if a jar exists on disk" (an OOM guard
        # from the Playwright era). That guard is obsolete and actively
        # harmful now:
        #   * the CDP driver runs ~310MB with ~200MB headroom, and the mint is
        #     a pure-Python HTTP call (gpsoauth) — it spawns no browser;
        #   * /app/sessions PERSISTS ACROSS DEPLOYS on Render, so the guard let
        #     an old jar win over a fresh master mint. PSIDTS rotates ~10min,
        #     so the service booted "logged out" (rotate=401, credits empty).
        # Now: mint fresh unless the on-disk jar is recent enough to trust.
        fp = ROOT / "sessions" / "flow.cookies.full.json"
        have_disk_jar = False
        jar_age_s = None
        if fp.exists():
            try:
                jar = json.loads(fp.read_text(encoding="utf-8"))
                if any(c.get("name") == "__Secure-1PSID" for c in jar):
                    have_disk_jar = True
                    jar_age_s = max(0.0, time.time() - fp.stat().st_mtime)
            except Exception:
                pass
        try:
            max_age = float(os.environ.get("FLOW_JAR_MAX_AGE_S", "1200"))
        except Exception:
            max_age = 1200.0
        force_mint = os.environ.get("FLOW_FORCE_MINT", "") == "1"
        jar_stale = (not have_disk_jar) or jar_age_s is None or \
                    jar_age_s > max_age
        if master and email and (jar_stale or force_mint or not self.jar):
            print(f"[auth] minting fresh jar (disk_jar={have_disk_jar} "
                  f"age={('%.0fs' % jar_age_s) if jar_age_s is not None else 'n/a'}"
                  f" force={force_mint})", flush=True)
            try:
                self.jar = _auth.mint_cookies(
                    email, master, os.environ.get("FLOW_ANDROID_ID",
                                                  "0123456789abcdef"))
                self.source = "master"
                self.minted = True
                print("[auth] jar MINTED from master token (unbound)", flush=True)
            except Exception as e:
                print(f"[auth] master mint failed: {str(e)[:200]}", flush=True)
        if not self.jar:
            env = os.environ.get("FLOW_COOKIES_JSON", "").strip()
            if env:
                try:
                    import base64
                    if not env.lstrip().startswith("["):
                        env = base64.b64decode(env).decode("utf-8")
                    jar = json.loads(env)
                    self.jar = {c["name"]: c["value"] for c in jar
                                if c.get("name")}
                    self.source = "env"
                except Exception:
                    pass
            if not self.jar and fp.exists():
                try:
                    jar = json.loads(fp.read_text(encoding="utf-8"))
                    self.jar = {c["name"]: c["value"] for c in jar
                                if c.get("name")}
                    self.source = "file"
                except Exception:
                    pass
        self.persist()
        print(f"[auth] jar loaded ({len(self.jar)} cookies, source={self.source})",
              flush=True)

    def persist(self):
        # sessions/ (engine seeds from it) + KV (survives deploys)
        try:
            fp = ROOT / "sessions" / "flow.cookies.full.json"
            fp.parent.mkdir(parents=True, exist_ok=True)
            # Make the records match the live jar BEFORE writing them: rotation
            # updates self.jar, and the engine seeds the browser + rebuilds
            # HTTP sessions from these records. Writing mint-time values here
            # shipped a dead PSIDTS to the browser on every re-seed.
            _auth.sync_records(self.jar)
            recs = _auth.load_records()
            if recs:
                fp.write_text(json.dumps(recs), encoding="utf-8")
            else:
                fp.write_text(json.dumps(
                    [{"name": k, "value": v, "domain": ".google.com", "path": "/",
                      "secure": (k.startswith("__Secure")
                                 or k.startswith("__Host") or k == "SAPISID")}
                     for k, v in self.jar.items()]), encoding="utf-8")
        except Exception:
            pass
        _auth.kv_put(self.jar, {"bl": self.bl, "fsid": self.fsid})

    def set_jar(self, jar):
        with self._lock:
            self.jar = jar
        self.persist()

    # -- rotation --------------------------------------------------------
    def rotate(self, force=False):
        if not self.jar:
            return False
        if not force and (time.time() - self.last_rotate) < 300:
            return True           # fresh enough
        with self._lock:
            try:
                code, ok = _auth.rotate_psidts(self.jar)
            except Exception:
                return False
        self.last_rotate = time.time()
        self.last_rotate_status = code
        self.last_rotate_ok = bool(ok)
        if ok:
            self.persist()
            print(f"[auth] PSIDTS rotated (status={code})", flush=True)
        else:
            print(f"[auth] rotate status={code} (refreshed={ok})", flush=True)
        return bool(ok)

    def start_loop(self):
        if self.loop is None and self.jar:
            self.loop = _auth.RotationLoop(
                lambda: self.jar, self.set_jar, interval=600,
                on_rotate=lambda ok, code: None)
            self.loop.start()

    # -- at token --------------------------------------------------------
    def ensure_at(self, max_age=1800):
        if self.at and (time.time() - self.at_ts) < max_age:
            return self.at
        if not self.jar:
            return ""
        try:
            s = _auth.restore_session(jar=self.jar)
            if not (self.bl and self.fsid):
                self.bl, self.fsid = _auth.extract_wiz(s)
            self.at = _auth.harvest_at(s, self.bl, self.fsid)
            self.at_ts = time.time()
            print(f"[auth] at harvested (len {len(self.at)})", flush=True)
            return self.at
        except Exception as e:
            print(f"[auth] at harvest failed: {str(e)[:150]}", flush=True)
            return ""

    def invalidate_at(self):
        self.at = ""
        self.at_ts = 0.0

    def info(self):
        return {
            "has_jar": bool(self.jar),
            "jar_source": self.source,
            "has_psidts": "__Secure-1PSIDTS" in self.jar,
            "has_at": bool(self.at),
            "at_age_s": round(time.time() - self.at_ts, 1) if self.at_ts else -1,
            "last_rotation_ago_s": (round(time.time() - self.last_rotate, 1)
                                    if self.last_rotate else -1),
            "last_rotate_status": self.last_rotate_status,
            "last_rotate_ok": self.last_rotate_ok,
            "minted": self.minted,
        }


AUTH = _AuthState()
AUTH.load()


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
        self.session_ok = None           # cached login state (None = unknown)
        self.last_recover = 0.0          # epoch of last successful self-heal
        self._supervisor = None
        self._supervisor_interval = int(os.environ.get("FLOW_SUPERVISE_S", "90"))

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

    def _kill_driver_procs(self):
        """SIGKILL playwright node drivers that belong to dead engines.
        Each leaked driver costs ~120MB — fatal on 512MB free tier."""
        import signal
        killed = 0
        for p in glob.glob("/proc/[0-9]*"):
            try:
                cmd = open(p + "/cmdline", "rb").read().replace(
                    b"\0", b" ").decode("utf-8", "replace")
                if ("node" in cmd and ("playwright" in cmd or "driver" in cmd)
                        and "package" in cmd):
                    os.kill(int(p.split("/")[-1]), signal.SIGKILL)
                    killed += 1
            except Exception:
                pass
        if killed:
            print(f"[*] SIGKILLed {killed} stale node driver(s)", flush=True)
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
                # TIMEOUT SAFETY: only hard-rebuild when THIS caller is the
                # only job. If other jobs are in flight, the worker is busy
                # (0.1 CPU makes everything slow) — rebuilding would kill
                # their pages mid-flight ('Target closed' crashes).
                with self._counter_lock:
                    others_active = self._active > 1
                if not _boot and not self._rebuild_inflight and not others_active:
                    try:
                        self.hard_rebuild()
                    except Exception as e:
                        print(f"[!] auto rebuild failed: {str(e)[:120]}", flush=True)
                elif others_active:
                    print("[!] engine job timed out but other jobs are "
                          "in-flight — skipping rebuild", flush=True)
                raise TimeoutError(f"engine job timed out after {timeout}s")
            if "err" in res:
                raise res["err"]
            self.last_engine_ok = time.time()
            return res.get("v")
        finally:
            with self._counter_lock:
                self._active -= 1

    def is_busy(self):
        # ANY in-flight job (>=1) means the worker is occupied — the old
        # '> 1' treated the running generate itself as "not busy", letting
        # the supervisor's liveness probe queue behind it, time out, and
        # hard-rebuild the browser OUT from under the running job.
        try:
            return self._active >= 1 or not self._q.empty()
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
                # Stop the old engine's playwright driver FIRST — its node
                # process (~120MB) otherwise leaks on every rebuild and the
                # leaked drivers pushed the container past 512MB.
                try:
                    if self.engine is not None and self.engine.pw is not None:
                        self.engine.pw.stop()
                except Exception:
                    pass
                self._kill_browser_procs()
                self._kill_driver_procs()
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
        # MEM: stop any previous engine's playwright driver + browser first —
        # replacing self.engine without stopping the old one leaks a full
        # stack (~315MB: chrome 110 + node driver 120 + buffers) per boot.
        try:
            if self.engine is not None and self.engine.pw is not None:
                self.engine.pw.stop()
        except Exception:
            pass
        self._kill_browser_procs()
        self._kill_driver_procs()
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
        # pure-Python auth upkeep starts regardless of the browser — but
        # DELAYED: at-harvest spawns a full TLS stack; firing it during the
        # browser's peak boot memory OOM-kills free tier. Supervisor pass #1
        # (90s) is early enough.
        try:
            if AUTH.jar:
                self._start_supervisor()
        except Exception:
            pass
        r = self._boot_engine()
        self._start_supervisor()
        return r

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

    # ── supervisor: keep engine alive + session logged in (never breaks) ──
    def ensure_session(self):
        """Worker-side: guarantee a logged-in session, self-healing by
        re-seeding FLOW_COOKIES_JSON / the on-disk export and rebuilding."""
        def job():
            return bool(self.engine and self.engine.ensure_session())
        try:
            ok = bool(self.run(job, timeout=300))
            self.session_ok = ok
            if ok:
                self.last_recover = time.time()
            return ok
        except Exception as e:
            self.session_ok = False
            print(f"[supervisor] ensure_session failed: {str(e)[:150]}", flush=True)
            return False

    def _start_supervisor(self):
        if self._supervisor and self._supervisor.is_alive():
            return
        self._supervisor = threading.Thread(target=self._supervise, daemon=True)
        self._supervisor.start()
        print(f"[*] supervisor on (every {self._supervisor_interval}s)", flush=True)

    def _supervise(self):
        """Forever: probe the engine, rebuild if wedged/dead, and periodically
        verify the login (re-seeding cookies if it lapsed). Also drives the
        pure-Python auth layer: PSIDTS rotation (10 min) + at refresh (30 min).
        Never rebuilds while a job is running.
        MEM-SPEC: also enforces the total process-tree memory guard —
        python + chrome RSS > FLOW_MEM_GUARD_MB (400) triggers a hard rebuild
        (fresh browser) instead of letting Render's OOM killer do it randomly."""
        boot_ts = time.time()
        last_sess = time.time() + 120   # first session check ~2min after boot
        last_leak_rebuild = 0.0
        while True:
            time.sleep(self._supervisor_interval)
            try:
                # ── pure-Python auth upkeep (no browser needed) ──
                if AUTH.jar:
                    AUTH.rotate()                    # ≥600s cadence internally
                    if (time.time() - boot_ts) > 120:
                        AUTH.ensure_at(max_age=1800)  # 30 min; skip boot race
                # ── MEM-SPEC watchdog: total tree RSS guard ──
                try:
                    mb = total_tree_rss_mb()
                    hard_danger = mb > max(MEM_GUARD_MB + 240, 640)
                    if (mb > MEM_GUARD_MB and not self.is_busy()) or hard_danger:
                        # Danger threshold: Render's kernel OOM-kills the
                        # whole container at 512MB. Dying mid-job loses the
                        # job anyway — so past a hard wall, rebuild even
                        # while busy instead of waiting for the kill.
                        if (time.time() - last_leak_rebuild > 600) or hard_danger:
                            print(f"[mem] RSS {mb:.0f} MB > guard "
                                  f"{MEM_GUARD_MB} MB — hard rebuild"
                                  f"{' (danger, busy)' if hard_danger else ''}",
                                  flush=True)
                            # breakdown so the next run is diagnosable
                            print(f"[mem] breakdown: {proc_breakdown()}", flush=True)
                            last_leak_rebuild = time.time()
                            self.hard_rebuild(boot=True)
                            import gc
                            gc.collect()
                            continue
                except Exception:
                    pass
                # ENGINE-MISSING takes priority over busy: a queued job with
                # no engine can never complete, so waiting on is_busy() here
                # deadlocked the service (busy=True forever, engine_alive
                # False, 429 guard rejecting everything).
                if self.engine is None or self.boot_error:
                    if time.time() - boot_ts > 60:
                        print("[supervisor] engine missing -> rebuild", flush=True)
                        self.hard_rebuild(boot=True)
                        continue
                if self.is_busy():
                    continue
                # give the boot engine time to settle before any rebuild
                if time.time() - boot_ts < 150:
                    continue
                # cheap liveness probe (refreshes last_engine_ok)
                try:
                    self.run(lambda: self.engine.page.evaluate("1"), timeout=60)
                except Exception as e:
                    print(f"[supervisor] liveness failed ({str(e)[:80]}) -> rebuild",
                          flush=True)
                    self.hard_rebuild(boot=True)
                    continue
                # login check every ~10 min
                if time.time() - last_sess > 600:
                    last_sess = time.time()
                    self.ensure_session()
            except Exception as e:
                print(f"[supervisor] error: {str(e)[:150]}", flush=True)

    # ── fast, non-blocking status (Render health checks) ─────────
    def _base_info(self):
        st = load_state()
        stale = (time.time() - self.last_engine_ok) > 180 if self.last_engine_ok else True
        rss = 0.0
        rss_py = read_rss_mb()
        try:
            rss = round(total_tree_rss_mb(), 1)
        except Exception:
            try:
                rss = round(rss_mb_of(str(PROFILE)), 1)
            except Exception:
                pass
        # NOTE: no CDP calls here — Playwright sync API is thread-bound to
        # the worker; calling it from the HTTP thread poisons the engine
        # (greenlet 'cannot switch to a different thread').
        return {
            "ok": True,
            "engine_alive": self.engine is not None,
            "engine_responsive": not stale,
            "engine": "chrome-headless-shell",
            "rss_total_mb": rss,
            "rss_python_mb": rss_py,
            "rss_chrome_mb": round(max(rss - rss_py, 0), 1),
            "stacks_est": round(max(rss, 1) / 320.0, 1),  # ~320MB per full stack
            "rss_mb": rss,          # backward compat
            "mem_guard_mb": MEM_GUARD_MB,
            "project": st.get("project_id", ""),
            "model": st.get("model", "BELUGA"),
            "busy": self.is_busy(), "local_run": self.local_run,
            "cookies_ready": (ROOT / "sessions" / "flow.cookies.full.json").exists()
                             or bool(os.environ.get("FLOW_COOKIES_JSON", "").strip()),
            "proxy": bool(os.environ.get("FLOW_PROXY_URL", "").strip()),
            "session_ok": self.session_ok,
            "last_recover_ago_s": (round(time.time() - self.last_recover, 1)
                                   if self.last_recover else -1),
            **AUTH.info(),
            "boot_error": self.boot_error,
            "last_error": ((getattr(self.engine, "last_error", "") if self.engine
                            else "") or self.boot_error),
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

    def diag(self):
        """Step-by-step remote self-test: does the browser boot, load Flow,
        spoof the fingerprint, mint a reCAPTCHA token, and read the account?
        Distinguishes {no engine, no network, not logged in, reCAPTCHA wall}."""
        def job():
            out: dict = {"steps": {}}
            eng = self.engine
            if eng is None:
                out["verdict"] = "no engine"
                out["steps"]["engine"] = {"ok": False, "error": self.boot_error}
                return out
            out["steps"]["engine"] = {"ok": True, "exe": eng.exe,
                                      "rss_mb": round(rss_mb_of(str(PROFILE)), 1)}
            out["steps"]["proxy"] = {"set": bool(os.environ.get("FLOW_PROXY_URL", "").strip())}
            try:
                url = eng.goto_app("/")
                out["steps"]["load"] = {"ok": True, "url": url}
            except Exception as e:
                out["steps"]["load"] = {"ok": False, "error": str(e)[:200]}
                out["verdict"] = "browser cannot load flow.google.com (network?)"
                return out
            try:
                fp = eng.page.evaluate("""() => ({
                    webdriver: navigator.webdriver,
                    plugins: (navigator.plugins||[]).length,
                    ua: navigator.userAgent,
                    brands: (navigator.userAgentData&&navigator.userAgentData.brands)||null,
                    url: location.href, title: document.title})""")
                out["steps"]["fingerprint"] = {"ok": fp.get("webdriver") in (None, False)
                                               and fp.get("plugins", 0) > 0, **fp}
            except Exception as e:
                out["steps"]["fingerprint"] = {"ok": False, "error": str(e)[:150]}
            try:
                tok = eng.mint("GENERATE")
                out["steps"]["mint"] = {"ok": bool(tok), "token_len": len(tok or "")}
            except Exception as e:
                out["steps"]["mint"] = {"ok": False, "error": str(e)[:150]}
            try:
                c = eng.credits()
                out["steps"]["credits"] = ({"ok": True, **c} if c else
                                           {"ok": False, "error": "no credits response "
                                            "(not logged in? cookies stale?)"})
            except Exception as e:
                out["steps"]["credits"] = {"ok": False, "error": str(e)[:150]}
            s = out["steps"]
            if not s.get("credits", {}).get("ok"):
                out["verdict"] = "logged out — seed FLOW_COOKIES_JSON / PROFILE_TAR_URL"
            elif not s.get("mint", {}).get("ok"):
                out["verdict"] = "logged in but reCAPTCHA mint fails (page/script issue)"
            else:
                out["verdict"] = ("logged in + minting. If /generate still returns "
                                  "UNUSUAL_ACTIVITY, egress IP is flagged — set "
                                  "FLOW_PROXY_URL to a residential proxy.")
            return out
        try:
            return self.run(job, timeout=150)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    # ── jobs ─────────────────────────────────────────────────────
    def reseed_cookies(self, cookies):
        """Accept a fresh cookie export, persist it, and rebuild the engine so
        the new session takes effect without a redeploy. Use when /diag reports
        logged-out. Body: the Cookie-Editor JSON array (or {"cookies": [...]})."""
        import json as _json
        if isinstance(cookies, dict):
            cookies = cookies.get("cookies") or []
        clean = [{"name": c.get("name"), "value": c.get("value"),
                  "domain": c.get("domain") or ".google.com",
                  "path": c.get("path", "/"),
                  "secure": bool(c.get("secure", True)),
                  "httpOnly": bool(c.get("httpOnly", False)),
                  "expirationDate": c.get("expirationDate") or c.get("expires")}
                 for c in (cookies or []) if c.get("name") and c.get("value")]
        if not clean:
            return {"ok": False, "error": "no usable cookies"}
        fp = ROOT / "sessions" / "flow.cookies.full.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        blob = _json.dumps(clean)
        fp.write_text(blob, encoding="utf-8")
        try:
            os.environ["FLOW_COOKIES_JSON"] = blob
        except Exception:
            pass
        try:
            (PROFILE / ".cookies_seeded").unlink()   # force re-seed
        except Exception:
            pass
        r: dict = self.hard_rebuild(boot=True)
        r["cookies"] = len(clean)
        return r

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
            # pre-flight auth upkeep: rotate PSIDTS if stale, refresh at if old
            try:
                if AUTH.jar:
                    AUTH.rotate()
                    AUTH.ensure_at(max_age=1800)
            except Exception:
                pass
            items = self.engine.generate(prompt, model=model, aspect=aspect,
                                         count=count, project_id=project_id,
                                         seed=seed)
            if not items:
                return {"ok": False,
                        "error": getattr(self.engine, "last_error", "")
                                 or "generation failed"}
            return {"ok": True, "media": items, **items[0]}
        return self._submit_job("generate", {
            "prompt": prompt, "model": model, "aspect": aspect,
            "seed": seed, "count": count, "project_id": project_id}, job)

    def submit_upscale(self, media_uuid, resolution="2K", project_id=None):
        def job():
            out = self.engine.upscale(media_uuid, resolution, project_id)
            if not out:
                return {"ok": False,
                        "error": getattr(self.engine, "last_error", "")
                                 or "upscale failed"}
            return {"ok": True, "source": media_uuid, "file": str(out)}
        return self._submit_job("upscale", {
            "media_uuid": media_uuid, "resolution": resolution,
            "project_id": project_id}, job)

    def generate(self, prompt, model=None, aspect=None, seed=None,
                 project_id=None, count=1):
        def job():
            try:
                if AUTH.jar:
                    AUTH.rotate()
                    AUTH.ensure_at(max_age=1800)
            except Exception:
                pass
            items = self.engine.generate(prompt, model=model, aspect=aspect,
                                         count=count, project_id=project_id,
                                         seed=seed)
            if not items:
                return {"ok": False,
                        "error": getattr(self.engine, "last_error", "")
                                 or "generation failed"}
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
            if c is None and AUTH.jar:
                # 401 from the engine -> invalidate the pure-HTTP at token so
                # the supervisor re-harvests; rotate once and retry via HTTP
                AUTH.invalidate_at()
                try:
                    if AUTH.rotate(force=True) and AUTH.ensure_at(max_age=0):
                        code, data = _auth.rpc(_auth._session(), "nzlxg", [],
                                               AUTH.at, AUTH.bl, AUTH.fsid)
                        if code == 200 and data:
                            AUTH.persist()
                            c = {"credits": data[0], "raw": data}
                except Exception:
                    pass
            return ({"ok": True, **c} if c
                    else {"ok": False, "error": "credits failed (login?)"})
        try:
            return self.run(job, timeout=90)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def projects(self, limit=30):
        def job():
            return {"ok": True, "projects": self.engine.projects(limit)}
        try:
            return self.run(job, timeout=60)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "projects": []}

    def create_project(self):
        def job():
            pid = self.engine.create_project()
            if pid:
                st = load_state()
                st["project_id"] = pid
                save_state(st)
                return {"ok": True, "project": pid}
            return {"ok": False,
                    "error": getattr(self.engine, "last_error", "") or "create failed"}
        try:
            return self.run(job, timeout=90)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def media(self, media_uuid):
        return None