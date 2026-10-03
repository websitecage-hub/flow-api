# -*- coding: utf-8 -*-
"""Google Flow terminal client — new backend (flow.google.com batchexecute).

Backend (reverse-engineered live 2026-09-11):
  App + API now live on https://flow.google.com (labs.google/fx 308-redirects
  here). All data flows through same-origin batchexecute:
    POST /u/1/_/AiSandboxAngularFrontend/data/batchexecute?rpcids=<id>
    body: f.req=<json>&at=<page token>
  Auth = flow.google.com cookies + `at` token scraped from page HTML.
  Generation stays reCAPTCHA-gated: only UI-triggered ogiZ0b requests pass
  (console-minted token + pure-HTTP replay -> PUBLIC_ERROR_UNUSUAL_ACTIVITY,
  verified). So generation drives the off-screen Chrome composer; the
  synchronous ogiZ0b 200 response already contains media uuids + signed
  CDN URLs (no polling needed).

RPC map (reports/rpc_map.txt, bootstrap_decoded.txt):
  ogiZ0b  image generation submit -> sync media + signed flow-content URLs
  Zzl0ze  project history (batches el1 + media el2)
  nzlxg   credits -> [50,3,8,1,null,50]
  ngNC2   generation defaults (narwhal_display / veo_3_1_lite)
  yBhWQ   video model availability | HTrJv app/model config
  WuwhI   analytics (ignore)

Commands (REPL):
  <prompt> | /model /aspect /seed /credits /history /download /upscale
  /options /open /projects /project /help /quit
"""
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).parent


def _load_dotenv():
    """Minimal .env loader (no dependency): KEY=VALUE lines, # comments."""
    try:
        for ln in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except Exception:
        pass


_load_dotenv()


def _detect_chrome():
    """Chrome/Chromium path: env > Windows > macOS > Linux. Returns '' if none."""
    env = os.environ.get("FLOW_CHROME", "").strip()
    if env and Path(env).exists():
        return env
    cands = []
    if sys.platform.startswith("win"):
        cands = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        cands = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                 "/usr/bin/chromium", "/usr/bin/chromium-browser",
                 "/snap/bin/chromium"]
        import shutil as _sh
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser"):
            w = _sh.which(name)
            if w:
                return w
        # Playwright-installed Chromium (Docker build runs
        # `playwright install chromium`, landing in ~/.cache/ms-playwright).
        import glob as _g
        home = os.path.expanduser("~")
        for pat in (f"{home}/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
                    f"{home}/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
                    "/ms-playwright/chromium-*/chrome-linux/chrome",
                    "/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell"):
            hits = sorted(_g.glob(pat), reverse=True)
            if hits:
                return hits[0]
    for c in cands:
        if c and Path(c).exists():
            return c
    return ""


PROFILE = Path(os.environ.get("FLOW_PROFILE", str(ROOT / "profile-copy")))
OUT_DIR = Path(os.environ.get("FLOW_OUT", str(ROOT / "outputs")))
STATE_FILE = ROOT / "sessions" / "flow_api_state.json"
COOKIES_FILE = ROOT / "sessions" / "flow.cookies.full.json"
LOG_FILE = ROOT / "flow.log"
PORT = int(os.environ.get("FLOW_PORT", "9333"))
CHROME = _detect_chrome()
SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
FLOW = "https://flow.google.com"
BE_RPC = FLOW + "/u/1/_/AiSandboxAngularFrontend/data/batchexecute"
PROJECT_URL = FLOW + "/u/1/project/{pid}"
BOOT_TIMEOUT = int(os.environ.get("FLOW_BOOT_TIMEOUT", "90"))
GEN_TIMEOUT = int(os.environ.get("FLOW_GEN_TIMEOUT", "120"))
HTTP_RETRIES = int(os.environ.get("FLOW_HTTP_RETRIES", "3"))
# Google-facing throttle (protects the account from Google rate limits)
MIN_SUBMIT_INTERVAL = int(os.environ.get("FLOW_MIN_SUBMIT_INTERVAL", "45"))
MAX_GENERATE_PER_DAY = int(os.environ.get("FLOW_MAX_GENERATE_PER_DAY", "200"))
MAX_UPSCALE_PER_DAY = int(os.environ.get("FLOW_MAX_UPSCALE_PER_DAY", "100"))
_CIRCUIT = {"fails": 0, "cooldown_until": 0.0}

ASPECTS = {"16:9": 3, "4:3": 5, "1:1": 1, "3:4": 4, "9:16": 2}
MODELS = ["NARWHAL", "HARBOR_SEAL", "GEM_PIX_2"]
MODEL_DISPLAY = {
    "NANO BANANA 2": "NARWHAL",
    "NANO BANANA 2 LITE": "HARBOR_SEAL",
    "NANO BANANA PRO": "GEM_PIX_2",
    "NARWHAL": "NARWHAL",
    "HARBOR_SEAL": "HARBOR_SEAL",
    "GEM_PIX_2": "GEM_PIX_2",
}


def norm_model(name):
    if not name:
        return "NARWHAL"
    key = str(name).strip().upper()
    return MODEL_DISPLAY.get(key, key)


def load_state():
    st = {}
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # env fallback so hosted/api-role instances work without the file
    st.setdefault("project_id", os.environ.get("FLOW_PROJECT", ""))
    st.setdefault("model", os.environ.get("FLOW_MODEL", "NARWHAL"))
    return st


def save_state(st):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def log(msg):
    """Timestamped line to console + flow.log + daily forever-log (JSONL)."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        import flow_store as _fs
        _fs.jlog("log", msg=msg[:300])
    except Exception:
        pass


def retryable(fn, tries=None, delay=2.0, what="op"):
    """Run fn(); retry on exception/None. Returns fn() result or None."""
    n = tries if tries is not None else HTTP_RETRIES
    last = None
    for i in range(max(1, n)):
        try:
            r = fn()
            if r is not None:
                return r
            last = None
        except Exception as e:  # noqa: BLE001
            last = e
        if i < n - 1:
            time.sleep(delay * (i + 1))
    if last is not None:
        log(f"[!] {what} failed after {n}x: {str(last)[:150]}")
    return None


def throttle(kind):
    """Google-facing rate limit. Returns (allowed, reason). Sleeps to honor
    MIN_SUBMIT_INTERVAL; enforces daily caps + failure circuit breaker."""
    import flow_store as _fs
    now = time.time()
    if now < _CIRCUIT["cooldown_until"]:
        wait = int(_CIRCUIT["cooldown_until"] - now)
        return False, f"circuit-breaker cooldown ({wait}s left)"
    cap = MAX_GENERATE_PER_DAY if kind == "generate" else MAX_UPSCALE_PER_DAY
    if _fs.submits_since(kind, 86400) >= cap:
        return False, f"daily {kind} cap reached ({cap})"
    last = _fs.last_submit_ts(kind)
    gap = now - last
    if last and gap < MIN_SUBMIT_INTERVAL:
        sleep_s = MIN_SUBMIT_INTERVAL - gap
        log(f"[*] throttle: waiting {sleep_s:.0f}s before {kind} (Google rate protection)")
        time.sleep(sleep_s)
    return True, ""


def throttle_note(kind, ok):
    """Record outcome: successful submit resets breaker; empty result trips it."""
    import flow_store as _fs
    if ok:
        _fs.submit_record(kind)
        _CIRCUIT["fails"] = 0
    else:
        _CIRCUIT["fails"] += 1
        if _CIRCUIT["fails"] >= 3:
            _CIRCUIT["cooldown_until"] = time.time() + 900
            _CIRCUIT["fails"] = 0
            log("[!] 3 straight empty results -> 15min cooldown (circuit breaker)")


def be_body(rpcid, payload, at):
    f_req = json.dumps([[[rpcid, json.dumps(payload, separators=(",", ":")),
                          None, "generic"]]], separators=(",", ":"))
    return urllib.parse.urlencode({"f.req": f_req, "at": at})


def be_parse(text):
    """Extract wrb.fr inners -> [(rpcid, data)]."""
    out = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln.startswith('[["wrb.fr"'):
            continue
        try:
            env = json.loads(ln)
            for chunk in env:
                if isinstance(chunk, list) and len(chunk) > 2 and chunk[0] == "wrb.fr":
                    rid, raw = chunk[1], chunk[2]
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = raw
                    out.append((rid, data))
        except Exception:
            pass
    return out


class PureHTTP:
    """Reads + downloads over pure HTTP (curl_cffi chrome146)."""

    def __init__(self):
        self.session = None
        self._at = ""
        self._at_ts = 0.0

    def _ses(self):
        if self.session is not None:
            return self.session
        from curl_cffi import requests as cffi
        s = cffi.Session(impersonate="chrome146")
        if COOKIES_FILE.exists():
            try:
                for c in json.loads(COOKIES_FILE.read_text(encoding="utf-8")):
                    dom = c.get("domain", "")
                    if "flow.google.com" in dom or dom.endswith("google.com"):
                        s.cookies.set(c["name"], c["value"], domain=dom,
                                      path=c.get("path", "/"))
            except Exception:
                pass
        self.session = s
        return s

    def reset(self):
        self.session = None
        self._at = ""

    def at(self, project_id, force=False):
        if self._at and not force and time.time() - self._at_ts < 600:
            return self._at

        def _fetch():
            r = self._ses().get(PROJECT_URL.format(pid=project_id), timeout=30)
            if r.status_code != 200:
                return None
            m = re.search(rb"AIQ-[A-Za-z0-9_\-]{20,80}:\d{10,20}", r.content)
            return m.group(0).decode() if m else None

        tok = retryable(_fetch, what="at-token scrape")
        if not tok:
            return ""
        self._at = tok
        self._at_ts = time.time()
        return self._at

    def call(self, rpcid, payload, project_id):
        def _once(fresh_at=False):
            at = self.at(project_id, force=fresh_at)
            if not at:
                return None
            u = (BE_RPC + "?rpcids=" + rpcid + "&source-path=" +
                 urllib.parse.quote(f"/u/1/project/{project_id}", safe=""))
            r = self._ses().post(
                u, data=be_body(rpcid, payload, at),
                headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                         "origin": FLOW, "referer": PROJECT_URL.format(pid=project_id)},
                timeout=60)
            if r.status_code != 200:
                return None
            for rid, data in be_parse(r.text):
                if rid == rpcid:
                    return data
            return None

        # try cached at, then one forced-refresh retry (self-heal on rotation)
        out = retryable(lambda: _once(False), tries=2, what=f"rpc {rpcid}")
        if out is None:
            out = retryable(lambda: _once(True), tries=2, what=f"rpc {rpcid} (fresh at)")
        return out

    # ── API ──────────────────────────────────────────────────────
    def credits(self):
        pid = load_state().get("project_id", "")
        if not pid:
            return None
        data = self.call("nzlxg", [], pid)
        try:
            return {"credits": data[0], "raw": data}
        except Exception:
            return None

    def history(self, project_id=None):
        pid = project_id or load_state().get("project_id", "")
        if not pid:
            return None
        data = self.call("Zzl0ze", [f"projects/{pid}", None, None, None, [1]], pid)
        if not isinstance(data, list):
            return None
        items = self._norm_history(data, pid)
        return items

    def _norm_history(self, data, pid):
        batches, media = {}, []
        try:
            for b in (data[1] or []):
                if not isinstance(b, list) or len(b) < 4:
                    continue
                info = b[3] or []
                batches[b[0]] = {
                    "title": info[0] if len(info) > 0 else "",
                    "created": self._ts(info[1]) if len(info) > 1 else "",
                    "media": info[4] if len(info) > 4 else "",
                }
            for m in (data[2] or []):
                if not isinstance(m, list) or len(m) < 6:
                    continue
                det = m[5] or []
                gen = m[6] if len(m) > 6 else []
                inner = {}
                try:
                    # det: [createTime, null x4, thumb, promptNest, null, null,
                    #       1, url, null, null, size]
                    # gen: [[null, seed, null x5, prompt, 29, "", null,
                    #        batch, null, url, 3], null, [w, h]]
                    pn = det[6] if len(det) > 6 else []
                    prompt = ""
                    try:
                        prompt = pn[2][0][0] or ""
                    except Exception:
                        pass
                    g0 = gen[0] if len(gen) > 0 else []
                    dims = gen[2] if len(gen) > 2 else []
                    inner = {"seed": g0[1] if len(g0) > 1 else None,
                             "prompt": prompt or (g0[7] if len(g0) > 7 else ""),
                             "url": det[10] if len(det) > 10 and isinstance(det[10], str) else "",
                             "width": dims[0] if len(dims) > 0 else None,
                             "height": dims[1] if len(dims) > 1 else None,
                             "size": det[13] if len(det) > 13 else None}
                except Exception:
                    pass
                thumb = det[5] if len(det) > 5 else ""
                media.append({
                    "uuid": m[0], "project": m[1], "batch": m[2],
                    "created": self._ts(det[0]) if det else "",
                    "prompt": inner.get("prompt") or "",
                    "seed": inner.get("seed"),
                    "url": inner.get("url") or "",
                    "thumb": thumb if isinstance(thumb, str) else "",
                    "size": inner.get("size"),
                    "width": inner.get("width"),
                    "height": inner.get("height"),
                    "title": (batches.get(m[2]) or {}).get("title", ""),
                })
        except Exception:
            pass
        media.sort(key=lambda x: x.get("created", ""), reverse=True)
        return media

    @staticmethod
    def _ts(t):
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t[0]))
        except Exception:
            return ""

    def defaults(self):
        pid = load_state().get("project_id", "")
        if not pid:
            return None
        return self.call("ngNC2", [f"tools/PINHOLE/projects/{pid}"], pid)

    def download_url(self, url, out_name=None):
        def _get():
            r = self._ses().get(url, timeout=90)
            if r.status_code != 200 or len(r.content) < 10000:
                return None
            return r.content

        b = retryable(_get, what="download")
        if not b:
            return None
        sig = b[:16]
        ext = ("jpg" if sig[:3] == b"\xff\xd8\xff"
               else "png" if sig[:8] == b"\x89PNG\r\n\x1a\n"
               else "webp" if sig[:4] == b"RIFF" else "bin")
        OUT_DIR.mkdir(exist_ok=True)
        out = OUT_DIR / (out_name or f"{uuid.uuid4()}.{ext}")
        if not str(out).endswith(f".{ext}"):
            out = out.with_suffix(f".{ext}")
        out.write_bytes(b)
        return out

    def download(self, media_uuid, project_id=None):
        # history URLs are auth-bound (302 -> login for plain HTTP);
        # use FlowEngine.download_media (CDP body capture) instead.
        return None


class FlowEngine:
    """Off-screen Chrome: types into the real composer (the only path the
    reCAPTCHA gate accepts) and reads the synchronous ogiZ0b response."""

    def __init__(self):
        self.browser = None
        self.page = None
        self.cdp = None
        self._gen_overrides = {}
        self._ogi_resps = []

    # ── lifecycle (self-healing boot) ────────────────────────────
    def start(self):
        import subprocess
        if not CHROME:
            log("[!] no Chrome/Chromium found. Set FLOW_CHROME=/path/to/chrome")
            sys.exit(1)
        if not PROFILE.exists():
            log(f"[!] profile not found at {PROFILE}")
            log("    copy your logged-in Chrome profile there, or set FLOW_PROFILE")
            sys.exit(1)
        # self-heal: if a healthy engine is already up, reuse it
        if self._cdp_alive():
            log("[*] engine already running — reusing")
        else:
            self._kill_leftovers()
            try:
                subprocess.Popen(self._launch_args())
            except Exception as e:
                log(f"[!] failed to launch Chrome: {e}")
                sys.exit(1)
            deadline = time.time() + BOOT_TIMEOUT
            while time.time() < deadline and not self._cdp_alive():
                time.sleep(1)
            if not self._cdp_alive():
                log("[!] engine Chrome did not start (profile lock? "
                    "kill leftover chrome and retry)")
                sys.exit(1)
        time.sleep(2)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log("[!] playwright not installed — run setup first "
                "(pip install -r requirements.txt)")
            sys.exit(1)
        try:
            self._pw = sync_playwright().start()
            self.browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{PORT}")
        except Exception as e:
            log(f"[!] CDP connect failed: {str(e)[:150]}")
            self._kill_leftovers()
            log("[!] killed leftovers; re-run to boot fresh")
            sys.exit(1)
        ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.page = self._pick_app_page(ctx) or (ctx.pages[0] if ctx.pages else ctx.new_page())
        self.cdp = self.page.context.new_cdp_session(self.page)
        self.cdp.send("Network.enable")
        self.page.route("**/batchexecute*ogiZ0b*", self._route_gen)
        self.page.on("response", self._on_resp)
        self._dump_cookies()
        if not self._session_ok():
            log("[*] session dead — trying auto-login once...")
            if not self.ensure_login():
                log("[!] not logged in. Sign in once (visible Chrome on the "
                    "profile), then re-run.")
        log("[*] engine ready")

    def restart(self):
        """Full self-heal: close everything and boot fresh."""
        log("[*] restarting engine...")
        try:
            self.close()
        except Exception:
            pass
        self.browser = None
        self.page = None
        self._ogi_resps = []
        self.start()

    def _pick_app_page(self, ctx):
        try:
            for p in ctx.pages:
                try:
                    u = p.url or ""
                except Exception:
                    continue
                if "flow.google.com" in u and "accounts.google" not in u \
                        and "/about" not in u:
                    return p
        except Exception:
            pass
        return None

    def _cdp_alive(self):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3)
            return True
        except Exception:
            return False

    def _kill_leftovers(self):
        """Kill stale engine Chromes (port/profile match) on any OS."""
        import subprocess
        try:
            if sys.platform.startswith("win"):
                subprocess.run(
                    ["powershell", "-Command",
                     "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                     "Where-Object { $_.CommandLine -like '*9333*' -or "
                     "$_.CommandLine -like '*profile-copy*' } | "
                     "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                    capture_output=True, timeout=30)
            else:
                subprocess.run(
                    f"pkill -f 'remote-debugging-port={PORT}'",
                    shell=True, capture_output=True, timeout=15)
                subprocess.run(
                    f"pkill -f '{PROFILE.name}'",
                    shell=True, capture_output=True, timeout=15)
        except Exception:
            pass
        time.sleep(2)

    def _launch_args(self):
        pid = load_state().get("project_id", "")
        start_url = PROJECT_URL.format(pid=pid) if pid else FLOW
        args = [CHROME, f"--remote-debugging-port={PORT}",
                f"--user-data-dir={str(PROFILE)}"]
        if sys.platform.startswith("win"):
            args.append("--window-position=-32000,-32000")
        else:
            # Headless container: no sandbox, small /dev/shm, no GPU.
            args += ["--headless=new", "--no-sandbox", "--disable-setuid-sandbox",
                     "--disable-dev-shm-usage", "--disable-gpu"]
        args += ["--disable-background-timer-throttling",
                 "--disable-backgrounding-occluded-windows",
                 "--disable-renderer-backgrounding",
                 "--no-first-run", "--no-default-browser-check",
                 start_url]
        return args

    def _dump_cookies(self):
        try:
            cookies = []
            for ctx in self.browser.contexts:
                for c in ctx.cookies():
                    cookies.append({"name": c["name"], "value": c["value"],
                                    "domain": c["domain"], "path": c.get("path", "/")})
            COOKIES_FILE.parent.mkdir(exist_ok=True)
            COOKIES_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _session_ok(self):
        try:
            pid = load_state().get("project_id", "")
            if not pid:
                return False
            return PureHTTP().history(pid) is not None
        except Exception:
            return False

    # ── auth fallback (kept, unreliable post-migration) ──────────
    CREDS_FILE = ROOT / "creds.json"

    def _creds(self):
        email = os.environ.get("FLOW_EMAIL", "").strip()
        pwd = os.environ.get("FLOW_PASSWORD", "").strip()
        if email and pwd:
            return email, pwd
        if self.CREDS_FILE.exists():
            try:
                j = json.loads(self.CREDS_FILE.read_text(encoding="utf-8"))
                return (j.get("email", "").strip(), j.get("password", ""))
            except Exception:
                pass
        return "", ""

    def ensure_login(self, timeout=240):
        email, pwd = self._creds()
        if not email or not pwd:
            return False
        page = self.page
        try:
            page.goto(FLOW + "/about", timeout=45000, wait_until="commit")
        except Exception:
            pass
        time.sleep(6)
        deadline = time.time() + timeout
        stage = "email"
        forced = False
        while time.time() < deadline:
            u = page.url or ""
            if "accounts.google" not in u and "flow.google.com" in u \
                    and "/about" not in u:
                print("[*] logged in ->", u[:80], flush=True)
                self._dump_cookies()
                return True
            try:
                if ("flow.google.com/about" in u or u.rstrip("/") == FLOW) \
                        and "accounts.google" not in u:
                    for sel in ("button:has-text('Create with Google Flow')",
                                "button:has-text('Try in Google Flow')"):
                        try:
                            b = page.locator(sel).first
                            if b.count() and b.is_visible():
                                b.click(timeout=10000)
                                time.sleep(6)
                                break
                        except Exception:
                            pass
                    u = page.url or ""
                if "confirmidentifier" in u and not forced:
                    try:
                        page.goto("https://accounts.google.com/v3/signin/identifier"
                                  "?continue=https://flow.google.com/"
                                  "&flowName=GlifWebSignIn&flowEntry=ServiceLogin"
                                  f"&Email={urllib.parse.quote(email)}",
                                  timeout=45000, wait_until="commit")
                    except Exception:
                        pass
                    forced = True
                    stage = "email"
                    time.sleep(5)
                    continue
                if stage == "email":
                    box = page.locator("#identifierId").first
                    if box.count() and box.is_visible():
                        try:
                            if email not in (box.input_value() or ""):
                                box.click()
                                box.fill(email)
                        except Exception:
                            box.click()
                            box.fill(email)
                        print("[*] submitted email", flush=True)
                        page.keyboard.press("Enter")
                        stage = "password"
                        time.sleep(4)
                        continue
                elif stage == "password":
                    boxes = page.locator("input[type=password]")
                    for j in range(boxes.count()):
                        bj = boxes.nth(j)
                        try:
                            if bj.is_visible():
                                bj.click()
                                bj.fill(pwd)
                                print("[*] submitted password", flush=True)
                                page.keyboard.press("Enter")
                                stage = "consent"
                                time.sleep(6)
                                break
                        except Exception:
                            pass
                    else:
                        pass
                    continue
                elif stage == "consent":
                    for txt in ["Not now", "Skip", "No thanks", "Cancel"]:
                        try:
                            loc = page.locator(f"button:has-text('{txt}')").first
                            if loc.count() and loc.is_visible():
                                loc.click(timeout=5000)
                                time.sleep(4)
                                break
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                page.evaluate("1")
            except Exception:
                pass
            time.sleep(2)
        self._dump_cookies()
        return self._session_ok()

    # ── generation rewrite (model/seed) ──────────────────────────
    def _route_gen(self, route):
        try:
            if not self._gen_overrides:
                route.continue_()
                return
            pd = route.request.post_data or ""
            q = dict(urllib.parse.parse_qsl(pd, keep_blank_values=True))
            fr = json.loads(q.get("f.req", "[]"))
            cell = fr[0][0]
            pay = json.loads(cell[1])
            req = pay[1][0]
            if "imageModelName" in self._gen_overrides:
                req[5] = self._gen_overrides["imageModelName"]
            if "seed" in self._gen_overrides:
                req[3] = int(self._gen_overrides["seed"])
            if "imageAspectRatio" in self._gen_overrides:
                req[4] = int(self._gen_overrides["imageAspectRatio"])
            pay[1][0] = req
            cell[1] = json.dumps(pay, separators=(",", ":"))
            q["f.req"] = json.dumps(fr, separators=(",", ":"))
            route.continue_(post_data=urllib.parse.urlencode(q))
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    def set_gen_overrides(self, **kw):
        for k, v in kw.items():
            if v is None:
                self._gen_overrides.pop(k, None)
            else:
                self._gen_overrides[k] = v

    def _on_resp(self, resp):
        try:
            if "rpcids=ogiZ0b" in resp.url and resp.status == 200:
                try:
                    self._ogi_resps.append(resp.body())
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def parse_ogi_response(body: bytes):
        """-> list of {uuid, batch, seed, prompt, url, width, height}."""
        out = []
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            return out
        dims = None
        try:
            for a, b in re.findall(r"\[(\d{3,5}),(\d{3,5})\]", text):
                a, b = int(a), int(b)
                if 100 <= a <= 5000 and 100 <= b <= 5000:
                    dims = (a, b)
                    break
        except Exception:
            pass
        for rid, data in be_parse(text):
            if rid != "ogiZ0b" or not isinstance(data, list):
                continue
            try:
                for entry in (data[0] if len(data) > 0 else []):
                    if not isinstance(entry, list) or len(entry) < 7:
                        continue
                    det = entry[6][0] if entry[6] else []
                    urls = re.findall(
                        r"https://flow-content\.google/image/[A-Za-z0-9_\-]+"
                        r"\?Expires=\d+&KeyName=[^&]+&Signature=[A-Za-z0-9_\-+/=]+",
                        json.dumps(data))
                    urls = [u.replace("\\u003d", "=").replace("\\u0026", "&")
                            for u in urls]
                    out.append({
                        "uuid": entry[0], "batch": entry[2],
                        "seed": det[1] if len(det) > 1 else None,
                        "prompt": det[7] if len(det) > 7 else "",
                        "url": urls[0] if urls else "",
                        "width": dims[0] if dims else None,
                        "height": dims[1] if dims else None,
                    })
            except Exception:
                pass
        return out

    # ── page helpers ─────────────────────────────────────────────
    def open_project(self, project_id, settle=8):
        if not project_id:
            log("[!] open_project: no project_id set")
            return False
        try:
            cur = self.page.url or ""
        except Exception:
            cur = ""
        if not isinstance(cur, str):
            cur = ""
        if project_id not in cur:
            ok = False
            for attempt in range(2):
                try:
                    self.page.goto(PROJECT_URL.format(pid=project_id),
                                   timeout=45000, wait_until="commit")
                    ok = True
                    break
                except Exception as e:
                    log(f"[*] goto retry {attempt + 1}/2: {str(e)[:100]}")
                    time.sleep(3)
            if not ok:
                return False
        # wait for app shell (composer) so callers never race page load;
        # skip the settle sleep if the composer is already live
        deadline = time.time() + 60
        live = False
        while time.time() < deadline:
            try:
                if self.page.locator("[contenteditable='true']").count() > 0:
                    live = True
                    break
            except Exception:
                pass
            time.sleep(2)
        if not live:
            time.sleep(settle)
        else:
            time.sleep(min(settle, 2))
        return True

    def _guardian(self, why="op"):
        """Pre-op self-check: engine alive? session valid?
        Heals (restart / cookie refresh) or returns False with guidance."""
        try:
            if not self._cdp_alive():
                log(f"[guardian] CDP dead before {why} -> restart")
                self.restart()
                if not self._cdp_alive():
                    return False
        except Exception as e:
            log(f"[guardian] restart failed: {str(e)[:120]}")
            return False
        try:
            pid = load_state().get("project_id", "")
            if pid and PureHTTP().history(pid) is not None:
                return True
            log(f"[guardian] session check failed before {why}; refreshing...")
            try:
                self._dump_cookies()
            except Exception:
                pass
            if pid and PureHTTP().history(pid) is not None:
                log("[guardian] session recovered after cookie refresh")
                return True
        except Exception as e:
            log(f"[guardian] check crashed: {str(e)[:120]}")
        log("[guardian] SESSION DEAD. Fix: boot visible Chrome on the profile "
            "(python flow_server.py serves /session with steps), sign in, retry.")
        return False

    def generate(self, prompt, model="NARWHAL", aspect=None, count=1,
                 project_id=None):
        """Generate `count` images (count = repeated UI submits, like UI xN).
        aspect: one of 16:9, 4:3, 1:1, 3:4, 9:16 (wire enum via route rewrite).
        Returns list of {uuid, batch, seed, prompt, url, width, height, file}."""
        project_id = project_id or load_state().get("project_id")
        try:
            count = max(1, min(4, int(count or 1)))
        except Exception:
            count = 1
        if aspect is not None:
            if isinstance(aspect, str) and aspect in ASPECTS:
                aspect = ASPECTS[aspect]
            try:
                self.set_gen_overrides(imageAspectRatio=int(aspect))
            except Exception:
                pass
        if model:
            self.set_gen_overrides(imageModelName=norm_model(model))
        if not self._guardian("generate"):
            return None
        allowed, reason = throttle("generate")
        if not allowed:
            log(f"[!] generate throttled: {reason}")
            return None

        def _attempt():
            try:
                return self._generate_loop(prompt, model, count, project_id)
            except Exception as e:  # noqa: BLE001
                log(f"[!] generate crashed ({type(e).__name__}): {str(e)[:150]}")
                return None

        out = _attempt()
        if out:
            return out
        # empty result (polluted composer, wedged tab, transient gate):
        # restart once (fresh page = clean composer) and retry once
        # (per-POST accounting + breaker already handled in _submit_collect)
        log("[!] generate empty -> self-heal restart + one retry")
        try:
            self.restart()
        except Exception as e2:
            log(f"[!] restart failed: {str(e2)[:150]}")
            return None
        return _attempt()

    def _generate_loop(self, prompt, model, count, project_id):
        if not self.open_project(project_id):
            return None
        if not self._set_ui_count(count):
            log(f"[*] count UI unset; single submit (UI default applies)")
        all_items = self._submit_collect(prompt, model, expect=count)
        if not all_items:
            return None
        http = PureHTTP()

        def _dl(it):
            out = http.download_url(it["url"], it["uuid"]) if it.get("url") else None
            it["file"] = str(out) if out else ""
            return it

        try:
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=min(4, len(all_items))) as ex:
                all_items = list(ex.map(_dl, all_items))
        except Exception:
            all_items = [_dl(it) for it in all_items]
        return all_items

    def _set_ui_count(self, count):
        """Set the composer xN toggle (the app fires N submits per Enter).
        Returns True when the chip confirms the requested count."""
        try:
            want = f"x{max(1, min(4, int(count)))}"
        except Exception:
            return False
        try:
            loc = self.page.locator("[role=button]", has_text="Nano Banana").first
            if loc.count() == 0:
                loc = self.page.locator("button", has_text="Nano Banana").first
            loc.click(timeout=8000)
            time.sleep(2)
            res = self.page.evaluate("""(t) => {
              const panes = [...document.querySelectorAll('.cdk-overlay-pane')];
              const root = panes.length ? panes[panes.length-1] : document;
              const els = [...root.querySelectorAll('button')];
              const el = els.find(e => (e.innerText || '').trim() === t ||
                (e.innerText || '').split('\\n').map(s=>s.trim()).includes(t));
              if (!el) return 'NOTFOUND';
              el.click();
              return 'ok';
            }""", want)
            time.sleep(1)
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(1)
            chip = self.page.evaluate("""() => {
              const els = [...document.querySelectorAll('[role=button],button')];
              const el = els.find(e => /Nano Banana/.test(e.innerText || ''));
              return el ? el.innerText.replace(/\\n/g,'|') : '';
            }""") or ""
            ok = f"|{want}" in chip or chip.endswith(want)
            log(f"[*] count -> {want} ({'ok' if ok else 'unverified: ' + chip[:40]})")
            return ok
        except Exception as e:
            log(f"[*] count set failed: {str(e)[:100]}")
            return False

    def _submit_collect(self, prompt, model="NARWHAL", expect=2):
        self._ogi_resps = []
        box = self.page.locator("[contenteditable='true']").first
        if box.count() == 0 or not box.is_visible():
            log("[!] composer not found")
            return None
        box.click()
        # clear leftovers so the prompt is exact; verify empty (chips may
        # resist Backspace -> fall back to Delete, up to 3 rounds)
        for _ in range(3):
            try:
                self.page.keyboard.press("ControlOrMeta+a")
                self.page.keyboard.press("Backspace")
                time.sleep(0.5)
                txt = (box.evaluate("e => e.innerText || \"\"") or "").strip()
                if not txt:
                    break
                self.page.keyboard.press("ControlOrMeta+a")
                self.page.keyboard.press("Delete")
                time.sleep(0.5)
            except Exception:
                break
        try:
            left = (box.evaluate("e => e.innerText || \"\"") or "").strip()
            if left:
                log(f"[!] composer not clearing (left: {left[:60]!r}); reloading page")
                self.page.reload(timeout=45000, wait_until="commit")
                time.sleep(8)
                box = self.page.locator("[contenteditable='true']").first
                box.click()
        except Exception:
            pass
        self.page.keyboard.type(prompt, delay=15)
        time.sleep(1.5)
        log(f"[*] generating ({model})...")
        # single logical submit; re-press Enter ONLY while nothing observed
        # (no request => no generation => no duplicate charge risk).
        # Capped: a polluted composer ignores Enters forever -> fail fast so
        # the caller can restart + retry instead of stacking state.
        deadline = time.time() + GEN_TIMEOUT
        last_press = 0.0
        presses = 0
        while time.time() < deadline and not self._ogi_resps:
            if time.time() - last_press > 12 and presses < 5:
                self.page.keyboard.press("Enter")
                last_press = time.time()
                presses += 1
            time.sleep(0.5)
            try:
                self.page.evaluate("1")
            except Exception:
                pass
        if not self._ogi_resps:
            log("[!] no generation request observed")
            throttle_note("generate", False)
            return None
        # twin responses arrive seconds apart; stop early once 2+ unique
        # items are parsed (min 10s), else collect until quiet
        uniq = []
        seen = set()
        t_first = 0.0
        quiet = time.time() + 20
        last_n = 0
        while time.time() < quiet:
            time.sleep(2)
            try:
                self.page.evaluate("1")
            except Exception:
                pass
            if len(self._ogi_resps) != last_n:
                last_n = len(self._ogi_resps)
                quiet = time.time() + 12
                if not t_first:
                    t_first = time.time()
                for b in self._ogi_resps:
                    for it in self.parse_ogi_response(b):
                        if it["uuid"] and it["uuid"] not in seen:
                            seen.add(it["uuid"])
                            uniq.append(it)
                if len(uniq) >= max(1, expect) and time.time() - t_first > 10:
                    break
        if not uniq:
            for b in self._ogi_resps:
                for it in self.parse_ogi_response(b):
                    if it["uuid"] and it["uuid"] not in seen:
                        seen.add(it["uuid"])
                        uniq.append(it)
        if not uniq:
            log("[!] empty generation response (gate error?)")
            throttle_note("generate", False)
            return None
        import flow_store as _fs
        for _ in self._ogi_resps:
            _fs.submit_record("generate")
        _CIRCUIT["fails"] = 0
        return uniq

    def upscale(self, media_uuid, resolution="2K", project_id=None):
        """UI-driven upscale: right-click card -> Download -> 2K/4K Upscaled.
        Captures the SPrCad 200 body (new media meta + inline base64 JPEG)
        and saves it. Verified: 1376x768 -> 2752x1536 (exactly 2x)."""
        pid = project_id or load_state().get("project_id", "")
        if not pid:
            print("[!] no project")
            return None
        if not self._guardian("upscale"):
            return None
        allowed, reason = throttle("upscale")
        if not allowed:
            log(f"[!] upscale throttled: {reason}")
            return None
        out = self._upscale_impl(media_uuid, resolution, pid)
        throttle_note("upscale", bool(out))
        return out

    def _upscale_impl(self, media_uuid, resolution, pid):
        key = "2K" if str(resolution).upper().startswith("2") else "4K"
        self.open_project(pid)
        n = 0
        for _ in range(30):
            try:
                n = self.page.evaluate(
                    "() => document.querySelectorAll('img[src*=\"/asb/\"]').length")
            except Exception:
                n = 0
            if n > 0:
                break
            time.sleep(3)
        if n == 0:
            print("[!] no media images visible")
            return None
        got = {}

        def _on_resp(resp):
            try:
                if "rpcids=SPrCad" in resp.url and resp.status == 200:
                    try:
                        got["body"] = resp.body()
                        print("[*] upscale response captured "
                              f"({len(got['body'])}B)", flush=True)
                    except Exception:
                        pass
            except Exception:
                pass

        self.page.on("response", _on_resp)
        items = PureHTTP().history(pid) or []
        want = [it for it in items
                if it["uuid"] == media_uuid or it["uuid"].startswith(media_uuid)]
        tokfrag = ""
        try:
            tokfrag = (want[0].get("url") or "").split("/asb/")[1][:24]
        except Exception:
            pass

        def _img_rect():
            return self.page.evaluate("""(tf) => {
              const imgs = [...document.querySelectorAll('img[src*="/asb/"]')];
              if (!imgs.length) return null;
              const img = tf ? (imgs.find(i => (i.src || '').includes(tf)) || imgs[0]) : imgs[0];
              const r = img.getBoundingClientRect();
              return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            }""", tokfrag)

        def _menu_click(pred):
            info = self.page.evaluate("""(predSrc) => {
              const els = [...document.querySelectorAll('[role=menuitem]')];
              const el = els.find(eval('(' + predSrc + ')'));
              if (!el) return null; const r = el.getBoundingClientRect();
              return {x: r.x + r.width / 2, y: r.y + r.height / 2,
                      w: r.width, h: r.height};
            }""", pred)
            if not info or info["w"] <= 0:
                return False
            self.page.mouse.click(info["x"], info["y"])
            return True

        try:
            ok_menu = False
            for attempt in range(3):
                rect = _img_rect()
                if not rect:
                    print("[!] media not found in grid")
                    return None
                try:
                    self.page.evaluate("""(tf) => {
                      const imgs = [...document.querySelectorAll('img[src*="/asb/"]')];
                      const img = tf ? (imgs.find(i => (i.src || '').includes(tf)) || imgs[0]) : imgs[0];
                      if (img) img.scrollIntoView({block: 'center'});
                    }""", tokfrag)
                    time.sleep(1)
                except Exception:
                    pass
                rect = _img_rect()
                self.page.mouse.click(rect["x"], rect["y"], button="right")
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        n = self.page.evaluate(
                            "() => [...document.querySelectorAll('[role=menuitem]')]"
                            ".filter(e => (e.innerText || '').toLowerCase().includes('download')).length")
                        if n > 0:
                            ok_menu = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.5)
                if ok_menu:
                    break
                print(f"[*] context menu missed (attempt {attempt + 1}/3)...", flush=True)
                try:
                    self.page.keyboard.press("Escape")
                except Exception:
                    pass
                time.sleep(1)
            if not ok_menu:
                print("[!] Download item not found")
                return None
            if not _menu_click("(e) => (e.innerText || '').toLowerCase().includes('download')"):
                print("[!] Download item not found")
                return None
            time.sleep(4)
            ok = _menu_click(
                "(e) => { const t = (e.innerText || '').toLowerCase();"
                f" return t.startsWith('{key.lower()}') && t.includes('upscal'); }}")
            if not ok:
                print(f"[!] {key} Upscaled item not found (paid gate?)")
                return None
            print(f"[*] {key} upscale requested...", flush=True)
            deadline = time.time() + 180
            while time.time() < deadline and "body" not in got:
                time.sleep(2)
                try:
                    self.page.evaluate("1")
                except Exception:
                    pass
            if "body" not in got:
                print("[!] upscale timed out")
                return None
            text = got["body"].decode("utf-8", "replace")
            parsed = be_parse(text)
            if not parsed:
                print("[!] unparseable upscale response")
                return None
            _rid, data = parsed[0]
            import base64 as _b64
            found_uuid, found_img = "", b""

            def _walk(o):
                nonlocal found_uuid, found_img
                if isinstance(o, list):
                    for v in o:
                        _walk(v)
                elif isinstance(o, str):
                    if not found_uuid and re.fullmatch(
                            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                            r"[0-9a-f]{4}-[0-9a-f]{12}", o):
                        found_uuid = o
                    elif len(o) > 100000 and o.startswith("/9j/"):
                        try:
                            raw = _b64.b64decode(o)
                        except Exception:
                            return
                        if raw[:3] == b"\xff\xd8\xff" and len(raw) > len(found_img):
                            found_img = raw

            _walk(data)
            if not found_img:
                print("[!] no image bytes in upscale response")
                return None
            new_id = found_uuid or f"{media_uuid}-upscaled"
            OUT_DIR.mkdir(exist_ok=True)
            out = OUT_DIR / f"{new_id}.jpg"
            out.write_bytes(found_img)
            print(f"[*] saved {out} ({len(found_img)} bytes, upsampled {media_uuid[:13]}..)")
            return out
        finally:
            try:
                self.page.remove_listener("response", _on_resp)
            except Exception:
                pass

    def download_media(self, media_uuid, project_id=None):
        """History re-download: image URLs are auth-bound for plain HTTP,
        so capture the bytes off the page pipe (CDP response bodies)."""
        pid = project_id or load_state().get("project_id", "")
        if not self._guardian("download"):
            return None
        items = PureHTTP().history(pid) or []
        want = [it for it in items
                if it["uuid"] == media_uuid or it["uuid"].startswith(media_uuid)]
        if not want:
            print("[!] uuid not in history")
            return None
        try:
            tok = (want[0].get("url") or "").split("/asb/")[1]
        except Exception:
            print("[!] no image url for uuid")
            return None
        got = {}

        def _on_resp(resp):
            try:
                if ("/asb/" in resp.url or "rd-asb" in resp.url) \
                        and tok[:24] in resp.url:
                    try:
                        bb = resp.body()
                    except Exception:
                        return
                    if len(bb) > 10000:
                        got["bytes"] = bb
            except Exception:
                pass

        self.page.on("response", _on_resp)
        try:
            self.open_project(pid)
            self.page.reload(timeout=45000, wait_until="commit")
        except Exception:
            pass
        deadline = time.time() + 60
        while time.time() < deadline and "bytes" not in got:
            time.sleep(2)
            try:
                self.page.evaluate("1")
            except Exception:
                pass
        try:
            self.page.remove_listener("response", _on_resp)
        except Exception:
            pass
        if "bytes" not in got:
            print("[!] image bytes not observed")
            return None
        b = got["bytes"]
        ext = ("jpg" if b[:3] == b"\xff\xd8\xff"
               else "png" if b[:8] == b"\x89PNG\r\n\x1a\n"
               else "webp" if b[:4] == b"RIFF" else "bin")
        OUT_DIR.mkdir(exist_ok=True)
        out = OUT_DIR / f"{want[0]['uuid']}.{ext}"
        out.write_bytes(b)
        print(f"[*] saved {out} ({len(b)} bytes)")
        return out

    def close(self):
        try:
            if self.page:
                self.page.close()
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if hasattr(self, "_pw"):
                self._pw.stop()
        except Exception:
            pass
        self._kill_leftovers()
        print("[*] engine closed")


class FlowAPI:
    """Serialized facade over engine + pure-HTTP (used by flow_server.py).

    ALL engine work runs on one dedicated worker thread (Playwright sync API
    is thread-bound); handlers submit callables via run(). Pure-HTTP reads
    also funnel through the worker for simplicity (curl is thread-safe)."""

    def __init__(self):
        import queue as _queue
        import threading as _threading
        self.engine = None
        self.http = PureHTTP()
        self.lock = _threading.Lock()
        self._q = _queue.Queue()
        self._ready = _threading.Event()
        self._threading = _threading
        self._active = 0
        # False on hosted API role: jobs only queue, a remote worker runs them
        self.local_run = True
        self.boot_error = ""

    def _worker(self):
        while True:
            fn, ev, res = self._q.get()
            try:
                res["v"] = fn()
            except BaseException as e:  # noqa: BLE001  (SystemExit is not Exception)
                res["err"] = e
            finally:
                ev.set()

    def run(self, fn, timeout=600):
        if not self._ready.is_set():
            raise RuntimeError("engine not started")
        with self.lock:
            self._active += 1
            try:
                ev = self._threading.Event()
                res = {}
                self._q.put((fn, ev, res))
                if not ev.wait(timeout):
                    raise TimeoutError("engine job timed out")
                if "err" in res:
                    err = res["err"]
                    if isinstance(err, SystemExit):
                        raise RuntimeError(
                            f"engine boot failed (exit {err.code}); "
                            "no Chrome/profile here? hosted role should be 'api'")
                    raise err
                if "v" not in res:
                    raise RuntimeError("engine task produced no result")
                return res["v"]
            finally:
                self._active -= 1

    def is_busy(self):
        try:
            return self._active > 1 or not self._q.empty()
        except Exception:
            return False

    def start(self, boot_engine=True):
        def _boot():
            if not boot_engine:
                return True
            self.engine = FlowEngine()
            self.engine.start()
            return True

        t = self._threading.Thread(target=self._worker, daemon=True)
        t.start()
        self._ready.set()
        self.boot_error = ""
        try:
            self.run(_boot, timeout=400)
        except Exception as e:  # noqa: BLE001
            # never let a boot failure kill the HTTP server: the API stays up
            # and /health reports the problem. (On Render ROLE=api this path is
            # not reached; on a misconfigured role it prevents exit(1) loops.)
            self.boot_error = str(e)[:300]
            print(f"[!] engine boot failed: {self.boot_error}", flush=True)
            return {"ok": False, "project": "", "error": self.boot_error}
        st = load_state()
        pid = st.get("project_id", "")
        # Only probe the live project when a real engine is present (local
        # role). On the hosted api role this would block boot on a slow Google
        # round-trip and stall Render's health check -> deploy timeout/fail.
        if self.engine is not None and pid:
            try:
                if self.http.history(pid) is None:
                    print(f"[*] project {pid[:13]}.. unreachable", flush=True)
                    pid = ""
            except Exception:
                pid = ""
        # Self-healing watchdog: if the headless Chrome dies, restart it. This
        # is what keeps the hosted engine alive across Render restarts.
        if boot_engine and self.engine is not None:
            wt = self._threading.Thread(target=self._watchdog, daemon=True)
            wt.start()
        return {"ok": True, "project": pid}

    def _watchdog(self):
        while True:
            time.sleep(60)
            try:
                if self.engine is not None and not self.engine._cdp_alive():
                    print("[watchdog] engine dead; restarting", flush=True)
                    try:
                        self.engine.restart()
                    except Exception as e:  # noqa: BLE001
                        print(f"[watchdog] restart failed: {str(e)[:120]}",
                              flush=True)
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        try:
            self.run(lambda: self.engine.close(), timeout=60)
        except Exception:
            pass

    def _submit_job(self, kind, params, fn, timeout=900):
        """Queue an engine job; returns job_id immediately. A daemon runner
        executes fn on the engine worker and records result into the DB."""
        import flow_store as _fs

        jid = _fs.new_job(kind, params)
        _fs.jlog("job_queued", job=jid, kind=kind)
        if not self.local_run:
            return jid  # hosted API role: remote worker claims via /worker/next

        def _runner():
            _fs.job_update(jid, status="running", event="started")
            try:
                res = self.run(fn, timeout=timeout)
                ok = not (isinstance(res, dict) and res.get("ok") is False)
                if isinstance(res, dict) and "media" in res:
                    try:
                        _fs.media_upsert(res["media"] if isinstance(
                            res["media"], list) else [res["media"]])
                    except Exception:
                        pass
                _fs.job_update(jid, status="done" if ok else "failed",
                               event="finished",
                               result=res if ok else None,
                               error=None if ok else (res.get("error") if isinstance(res, dict) else "failed"))
                _fs.jlog("job_done", job=jid, ok=ok)
            except Exception as e:  # noqa: BLE001
                _fs.job_update(jid, status="failed", event="crashed",
                               error=str(e)[:300])
                _fs.jlog("job_failed", job=jid, error=str(e)[:200])

        t = self._threading.Thread(target=_runner, daemon=True)
        t.start()
        return jid

    def submit_generate(self, prompt, model=None, aspect=None, seed=None,
                        project_id=None, count=1):
        def job():
            if model:
                self.engine.set_gen_overrides(imageModelName=norm_model(model))
            else:
                self.engine.set_gen_overrides(imageModelName=None)
            if aspect:
                self.engine.set_gen_overrides(imageAspectRatio=aspect)
            else:
                self.engine.set_gen_overrides(imageAspectRatio=None)
            if seed is not None:
                try:
                    self.engine.set_gen_overrides(seed=int(seed))
                except Exception:
                    pass
            else:
                self.engine.set_gen_overrides(seed=None)
            items = self.engine.generate(prompt, model or "NARWHAL",
                                         aspect=aspect, count=count,
                                         project_id=project_id)
            if not items:
                return {"ok": False, "error": "generation failed"}
            first = items[0]
            return {"ok": True, "uuid": first.get("uuid"),
                    "seed": first.get("seed"), "prompt": first.get("prompt"),
                    "width": first.get("width"), "height": first.get("height"),
                    "file": first.get("file"),
                    "files": [it.get("file") for it in items],
                    "media": items}
        return self._submit_job("generate",
                                {"prompt": prompt, "model": model,
                                 "aspect": aspect, "seed": seed,
                                 "count": count, "project_id": project_id},
                                job)

    def submit_upscale(self, media_uuid, resolution="2K", project_id=None):
        def job():
            out = self.engine.upscale(media_uuid, resolution, project_id)
            if not out:
                return {"ok": False, "error": "upscale failed"}
            return {"ok": True, "source": media_uuid, "file": str(out)}
        return self._submit_job("upscale",
                                {"media_uuid": media_uuid,
                                 "resolution": resolution,
                                 "project_id": project_id}, job)

    def _base_info(self):
        """Fast, non-blocking snapshot (no Google call, no worker queue).
        Safe to serve on every health check: Render kills deploys when the
        health path stalls on a slow upstream."""
        st = load_state()
        alive = False
        try:
            alive = bool(self.engine) and self.engine._cdp_alive()
        except Exception:
            alive = False
        pid = st.get("project_id", "")
        cookies_ready = COOKIES_FILE.exists()
        return {"ok": True, "engine_alive": alive,
                "project": pid, "model": st.get("model", "NARWHAL"),
                "busy": self.is_busy(), "local_run": self.local_run,
                "cookies_ready": cookies_ready,
                "boot_error": getattr(self, "boot_error", "")}

    def session_info(self):
        """Deep session check (live Google probe). Slow by nature; use /health
        for liveness and this /session only when a real answer is needed."""
        def job():
            out = self._base_info()
            sess = False
            try:
                pid = out.get("project", "")
                sess = bool(pid) and self.http.history(pid) is not None
            except Exception:
                sess = False
            out["session_ok"] = sess
            if not sess:
                out["login_steps"] = [
                    "1. Run: python -u -c \"from flow_api import FlowEngine; "
                    "e=FlowEngine(); e.start()\"  (or keep server up)",
                    "2. In another shell find the visible Chrome profile dir "
                    "(FLOW_PROFILE) and open Chrome on it visibly:",
                    "   chrome --user-data-dir=<FLOW_PROFILE> https://flow.google.com/about",
                    "3. Click 'Create with Google Flow', sign in, finish the wizard,",
                    "   open any project so flow.google.com/u/1/project/<id> loads.",
                    "4. Re-run: session check passes automatically (guardian).",
                ]
            return out
        return self.run(job, timeout=120)

    def status(self):
        """Fast health snapshot for /health (never blocks on Google)."""
        return self._base_info()

    def restart(self):
        def job():
            self.engine.restart()
            self.http.reset()
            return {"ok": True}
        return self.run(job, timeout=600)

    def generate(self, prompt, model=None, aspect=None, seed=None,
                 project_id=None, count=1):
        def job():
            if model:
                self.engine.set_gen_overrides(imageModelName=norm_model(model))
            else:
                self.engine.set_gen_overrides(imageModelName=None)
            if aspect:
                self.engine.set_gen_overrides(imageAspectRatio=aspect)
            else:
                self.engine.set_gen_overrides(imageAspectRatio=None)
            if seed is not None:
                try:
                    self.engine.set_gen_overrides(seed=int(seed))
                except Exception:
                    pass
            else:
                self.engine.set_gen_overrides(seed=None)
            items = self.engine.generate(prompt, model or "NARWHAL",
                                         aspect=aspect, count=count,
                                         project_id=project_id)
            if not items:
                return {"ok": False, "error": "generation failed"}
            first = items[0]
            return {"ok": True, "uuid": first.get("uuid"),
                    "seed": first.get("seed"), "prompt": first.get("prompt"),
                    "width": first.get("width"), "height": first.get("height"),
                    "file": first.get("file"),
                    "files": [it.get("file") for it in items],
                    "media": items}
        return self.run(job, timeout=600)

    def upscale(self, media_uuid, resolution="2K", project_id=None):
        def job():
            out = self.engine.upscale(media_uuid, resolution, project_id)
            if not out:
                return {"ok": False, "error": "upscale failed"}
            return {"ok": True, "source": media_uuid, "file": str(out)}
        return self.run(job, timeout=600)

    def history(self, project_id=None):
        def job():
            import flow_store as _fs
            pid = project_id or load_state().get("project_id", "")
            if not pid:
                return {"ok": False, "error": "no project set"}
            media = self.http.history(pid)
            if media is None:
                # resilience: serve last-known mirror, flagged stale
                cached = _fs.media_list(limit=100)
                if cached:
                    return {"ok": True, "project": pid, "media": cached,
                            "stale": True}
                return {"ok": False, "error": "history failed (stale session)"}
            try:
                _fs.media_upsert(media)
            except Exception:
                pass
            return {"ok": True, "project": pid, "media": media}
        return self.run(job)

    def credits(self):
        def job():
            c = self.http.credits()
            if c is None:
                return {"ok": False, "error": "credits failed"}
            return {"ok": True, "credits": c.get("credits"), "raw": c.get("raw")}
        return self.run(job)

    def projects(self):
        return {"ok": False, "error": "project listing not mapped on new backend"}

    def create_project(self):
        return None

    def media(self, media_uuid):
        def job():
            st = load_state()
            return self.engine.download_media(media_uuid, st.get("project_id", ""))
        return self.run(job, timeout=300)


if __name__ == "__main__":
    print("flow_api is a library. Run: python flow.py (menu) | "
          "python gen.py \"...\" | python flow_server.py")

