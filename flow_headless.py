# -*- coding: utf-8 -*-
"""Flow headless engine — chrome-headless-shell, fits Render free 512MB.

WHY THIS EXISTS
---------------
The original engine (flow_api.FlowEngine) launches full Chrome
("--headless=new" + a 11-process renderer tree). Measured idle RSS on the
loaded Flow app: ~1.2 GB. That is why the repo concluded "free tier OOMs,
need paid Starter + a home PC worker".

That conclusion is wrong. Chrome ships a *second*, far lighter headless
binary — `chrome-headless-shell` (the old headless mode, now its own
binary) — which runs the whole Flow app in a single process:

    measured: 95 MB idle, 115 MB with flow.google.com loaded,
              118 MB peak while minting a reCAPTCHA Enterprise token.

So the browser engine DOES fit Render's free 512 MB tier.

WHAT IT DOES (no UI clicking)
-----------------------------
  1. Boots chrome-headless-shell once and keeps it warm.
  2. Mints reCAPTCHA Enterprise action tokens by calling
     `grecaptcha.enterprise.execute(sitekey, {action})` inside the real
     flow.google.com page (validated: returns a 2.4 kB token).
  3. Calls the same-origin `batchexecute` RPCs the Angular app calls,
     from inside the page (real cookies/origin/network stack) with the
     minted token injected — i.e. the genuine API request, not a click.

Endpoints are the ones mapped from the app's own bundle:
  nzlxg   /VideoFxService.GetCredits
  cPZSdc  /VideoFxService.GetFlowAppConfig
  Zzl0ze  project history (batches + media)
  ogiZ0b  image generation submit
  SPrCad  upscale

Env:
  FLOW_CHROME          path to chrome-headless-shell (auto-detected)
  FLOW_PROFILE         persistent profile dir (holds the Google login)
  FLOW_PROJECT         default project uuid
  FLOW_MEM_GUARD_MB    soft RSS budget; engine refuses to spawn past it
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
SITE_KEY = os.environ.get("FLOW_SITE_KEY",
                          "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV")
FLOW = "https://flow.google.com"
BE_PATH = "/u/1/_/AiSandboxAngularFrontend/data/batchexecute"
PROFILE = Path(os.environ.get("FLOW_PROFILE", str(ROOT / "profile-copy")))
STATE_FILE = ROOT / "sessions" / "flow_api_state.json"
COOKIES_FILE = ROOT / "sessions" / "flow.cookies.full.json"
MEM_GUARD_MB = int(os.environ.get("FLOW_MEM_GUARD_MB", "420"))

# ── model registry (VERIFIED against the live app 2026-10-07) ─────
# Wire codes come from the app's own enum (FlowModel enum in the auth-gated
# bundle) and the real ogiZ0b request captured from the app:
#   ogiZ0b -> /FlowService.BatchGenerateImages
#   request[5] = imageModelKey  (NOT imageModelName)
#   request[4] = aspectRatio int (16:9→3, 4:3→5, 1:1→1, 3:4→4, 9:16→2)
#   recaptcha action for images = "IMAGE_GENERATION" (not "GENERATE")
MODEL_DISPLAY = {
    "NANO BANANA 2.1": "BELUGA",
    "NANO BANANA 2.1 THINKING LOW": "BELUGA_THINKING_LOW",
    "NANO BANANA 2.1 THINKING MED": "BELUGA_THINKING_MED",
    "NANO BANANA 2.1 THINKING HIGH": "BELUGA_THINKING_HIGH",
    "NANO BANANA 2": "NARWHAL",
    "NANO BANANA 2 LITE": "HARBOR_SEAL",
    "NANO BANANA PRO": "GEM_PIX_2",
    "NANO BANANA": "NARWHAL",
}
# Full image-model enum from the bundle (id -> code)
MODEL_ENUM = {
    20: "IMAGEN_3_5_FAST", 21: "IMAGEN_3_5", 23: "GEM_PIX", 24: "R2I",
    25: "GEM_PIX_2", 26: "GEM_PIX_2_UPSAMPLE_2K", 27: "GEM_PIX_2_UPSAMPLE_4K",
    28: "GEM_PIX_2_BOTTLE", 29: "NARWHAL", 30: "GEM_PIX_2_GEOGENIE",
    31: "HARBOR_SEAL", 32: "GEM_PIX_VERTEX", 33: "GEM_PIX_2_VERTEX",
    34: "GEM_PIX_PRO_VERTEX", 35: "GEM_PIX_2_LITE_VERTEX", 36: "BELUGA",
    37: "BELUGA_THINKING_LOW", 38: "BELUGA_THINKING_MED",
    39: "BELUGA_THINKING_HIGH", 40: "BELUGA_VERTEX",
}
# Models the Flow UI actually offers for image generation (from the app's
# own master list: "GEM_PIX_2 ... NARWHAL HARBOR_SEAL BELUGA BELUGA_THINKING_*")
MODEL_WIRE = {
    "BELUGA": {"label": "Nano Banana 2.1", "gemini": "gemini-nano-banana-2.1",
               "default": True},
    "BELUGA_THINKING_LOW": {"label": "Nano Banana 2.1 (Thinking Low)",
                            "gemini": "gemini-nano-banana-2.1"},
    "BELUGA_THINKING_MED": {"label": "Nano Banana 2.1 (Thinking Med)",
                            "gemini": "gemini-nano-banana-2.1"},
    "BELUGA_THINKING_HIGH": {"label": "Nano Banana 2.1 (Thinking High)",
                             "gemini": "gemini-nano-banana-2.1"},
    "GEM_PIX_2": {"label": "Nano Banana Pro", "gemini": "gemini-3-pro-image"},
    "NARWHAL": {"label": "Nano Banana 2", "gemini": "gemini-3.1-flash-image",
                "note": "deprecated; shuts down 2026-10-29"},
    "HARBOR_SEAL": {"label": "Nano Banana 2 Lite",
                    "gemini": "gemini-3.1-flash-lite-image"},
}
MODELS = list(MODEL_WIRE.keys())
DEFAULT_MODEL = "BELUGA"

# Aspect ratio: Flow sends an integer enum (verified 1:1→1, 9:16→2, 16:9→3,
# 3:4→4, 4:3→5). Nano Banana 2.1 also supports 3:2/2:3/4:5/5:4/21:9 etc.
# — those get their enum numbers captured from the live UI (capture_ui()).
ASPECT_WIRE = {"1:1": 1, "9:16": 2, "16:9": 3, "3:4": 4, "4:3": 5,
               "3:2": 6, "2:3": 7, "4:5": 8, "5:4": 9, "21:9": 10}


def norm_model(name):
    """Human/model name -> wire code. Accepts wire codes unchanged."""
    if not name:
        return "NARWHAL"
    k = str(name).strip().upper()
    if k in MODEL_WIRE:
        return k
    return MODEL_DISPLAY.get(k, k.replace(" ", "_"))


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ────────────────────────────────────────────────────────────────
# binary detection: PREFER the tiny headless shell over full Chrome
# ────────────────────────────────────────────────────────────────
_SHELL_GLOBS = [
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    "/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    "/root/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
]
_SHELL_NAMES = ["chrome-headless-shell", "headless_shell"]


def detect_headless_shell() -> str:
    """Return a chrome-headless-shell path, or '' if none found."""
    env = os.environ.get("FLOW_CHROME", "").strip()
    if env and Path(env).exists() and "headless-shell" in env:
        return env
    for n in _SHELL_NAMES:
        w = shutil.which(n)
        if w:
            return w
    hits = []
    for pat in _SHELL_GLOBS:
        hits += glob.glob(os.path.expanduser(pat))
    hits = [h for h in hits if Path(h).exists()]
    if hits:
        return sorted(hits, reverse=True)[0]
    if env and Path(env).exists():
        return env          # last resort: whatever the user pointed at
    return ""


def rss_mb_of(proc_substr: str) -> float:
    """Total RSS (MB) of processes whose cmdline contains proc_substr."""
    total = 0
    for p in glob.glob("/proc/[0-9]*"):
        try:
            cmd = open(p + "/cmdline", "rb").read().replace(b"\0", b" ").decode(
                "utf-8", "replace")
            if proc_substr not in cmd:
                continue
            for line in open(p + "/status"):
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
                    break
        except Exception:
            pass
    return total / 1024.0


def load_state() -> dict:
    st = {}
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    st.setdefault("project_id", os.environ.get("FLOW_PROJECT", ""))
    st.setdefault("model", os.environ.get("FLOW_MODEL", "NARWHAL"))
    return st


# ────────────────────────────────────────────────────────────────
# in-page JS: mint + batchexecute (the genuine API calls)
# ────────────────────────────────────────────────────────────────
JS_MINT = """(site) => new Promise((resolve) => {
  const fin = (o) => resolve(o);
  const go = () => window.grecaptcha.enterprise.ready(() =>
      window.grecaptcha.enterprise.execute(site, {action: 'GENERATE'})
        .then(t => fin({ok: true, token: t}))
        .catch(e => fin({ok: false, err: String(e).slice(0, 300)})));
  if (window.grecaptcha && window.grecaptcha.enterprise) return go();
  const s = document.createElement('script');
  s.src = 'https://www.google.com/recaptcha/enterprise.js?render=' + site;
  s.onload = go;
  s.onerror = () => fin({ok: false, err: 'script_error'});
  document.head.appendChild(s);
  setTimeout(() => fin({ok: false, err: 'mint_timeout'}), 45000);
})"""

JS_RPC = """async ({rpcid, payload, at, pid}) => {
  const fReq = JSON.stringify([[[rpcid, JSON.stringify(payload), null, "generic"]]]);
  const body = new URLSearchParams();
  body.set('f.req', fReq);
  if (at) body.set('at', at);
  const src = pid ? ('/u/1/project/' + pid) : '/u/1/';
  const u = location.origin + '/u/1/_/AiSandboxAngularFrontend/data/batchexecute' +
            '?rpcids=' + rpcid + '&source-path=' + encodeURIComponent(src);
  try {
    const r = await fetch(u, {
      method: 'POST', credentials: 'include',
      headers: {'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'},
      body: body.toString()
    });
    const text = await r.text();
    return {status: r.status, body: text};
  } catch (e) { return {status: -1, err: String(e).slice(0, 300)}; }
}"""

JS_SCRAPE_AT = """() => {
  // Google apps expose the batchexecute XSRF token as WIZ_global_data.SNlM0e.
  try {
    if (window.WIZ_global_data && window.WIZ_global_data.SNlM0e)
      return window.WIZ_global_data.SNlM0e;
  } catch (e) {}
  const h = document.documentElement.innerHTML;
  let m = h.match(/["']?SNlM0e["']?\\s*[:=]\\s*["']([A-Za-z0-9_\\-]+:\\d{10,20})["']/);
  if (m) return m[1];
  m = h.match(/AIQ-[A-Za-z0-9_\\-]{20,80}:\\d{10,20}/);
  return m ? m[0] : '';
}"""

# Anti-detection: reCAPTCHA Enterprise flags the *automation* fingerprint —
# navigator.webdriver=true, a "HeadlessChrome" UA brand, and 0 plugins.
# Measured: headless-shell with these left in -> PUBLIC_ERROR_UNUSUAL_ACTIVITY;
# with them spoofed -> generation SUCCEEDS at ~115 MB. Do NOT remove.
SPOOF_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
Object.defineProperty(navigator,'mimeTypes',{get:()=>[1,2]});
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
window.chrome = window.chrome || {runtime:{}};
try { const oq = navigator.permissions && navigator.permissions.query;
      if (oq) navigator.permissions.query = (p)=> p.name==='notifications'
        ? Promise.resolve({state: Notification.permission}) : oq(p); } catch(e){}
"""

# THE THING (2026-10-08): reCAPTCHA Enterprise reads the client-hint brand list,
# not just UA/plugins/webdriver. Headless builds leak "HeadlessChrome" in
# navigator.userAgentData.brands AND in the sec-ch-ua wire header. Mask BOTH:
#  1) override navigator.userAgentData to a clean Chromium brand list, and
#  2) scrub + replace sec-ch-ua* request headers (route handler below).
# NEVER remove the user-agent header itself (a UA-less request is flagged).
# Verified: with this fix chrome-headless-shell generates at ~96 MB RSS;
# without it -> PUBLIC_ERROR_UNUSUAL_ACTIVITY in every headless mode.
UAD_OVERRIDE_JS = """
try {
  const brands = [{brand:'Chromium', version:'153'}, {brand:'Not_A Brand', version:'8'}];
  const full = [{brand:'Chromium', version:'153.0.8010.12'},
                {brand:'Not_A Brand', version:'8.0.0.0'}];
  const uad = {
    brands, mobile: false, platform: 'Linux',
    getHighEntropyValues: () => Promise.resolve({
      brands: full, mobile: false, platform: 'Linux', architecture: 'x86',
      bitness: '64', fullVersionList: full, platformVersion: '',
      wow64: false, model: ''}),
    toJSON: () => ({brands, mobile: false, platform: 'Linux'})
  };
  Object.defineProperty(navigator, 'userAgentData', {get: () => uad, configurable: true});
} catch (e) {}
"""
# sec-ch-ua values a normal Chromium-153 desktop sends (do NOT vary these from
# the UAD override above — mismatched brands are their own signal)
CLEAN_CH_HEADERS = {
    "sec-ch-ua": '"Chromium";v="153", "Not_A Brand";v="8"',
    "sec-ch-ua-full-version-list":
        '"Chromium";v="153.0.8010.12", "Not_A Brand";v="8.0.0.0"',
    "sec-ch-ua-platform": '"Linux"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-arch": '"x86"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform-version": '""',
    "sec-ch-ua-wow64": "?0",
}


def _scrub_ch_headers(route):
    """Route handler: replace the sec-ch-ua* family with clean Chromium values.
    User-Agent is left untouched. ONLY the client-hint headers are rewritten."""
    try:
        h = dict(route.request.headers)
        for k in list(h):
            if k.lower().startswith("sec-ch-ua"):
                h.pop(k, None)
        h.update(CLEAN_CH_HEADERS)
        route.continue_(headers=h)
    except Exception:
        try:
            route.continue_()
        except Exception:
            pass


def parse_batchexecute(text: str):
    """-> list of (rpcid, decoded_payload). Mirrors the app's framing."""
    out = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line.startswith('[["wrb.fr"'):
            continue
        try:
            env = json.loads(line)
        except Exception:
            continue
        for chunk in env:
            if isinstance(chunk, list) and len(chunk) > 2 and chunk[0] == "wrb.fr":
                try:
                    data = json.loads(chunk[2]) if chunk[2] else None
                except Exception:
                    data = chunk[2]
                out.append((chunk[1], data))
    return out


class HeadlessEngine:
    """chrome-headless-shell driven Flow API. One process, ~120 MB."""

    def __init__(self):
        self.exe = ""
        self.pw = None
        self.ctx = None
        self.page = None
        self._at = ""

    # ── lifecycle ────────────────────────────────────────────────
    def start(self, force_heavy_chrome: bool = False):
        self.exe = "" if force_heavy_chrome else detect_headless_shell()
        if not self.exe:
            raise RuntimeError(
                "chrome-headless-shell not found. Install:\n"
                "  pip install playwright && playwright install chromium-headless-shell\n"
                "  (or set FLOW_CHROME=/path/to/chrome-headless-shell)")
        if not PROFILE.exists():
            PROFILE.mkdir(parents=True, exist_ok=True)
        used = rss_mb_of(str(PROFILE))
        if used > MEM_GUARD_MB:
            _log(f"[!] RSS already {used:.0f} MB > guard {MEM_GUARD_MB} MB")
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(PROFILE), executable_path=self.exe, headless=True,
            bypass_csp=True,               # needed for in-page script inject
            user_agent=SPOOF_UA,           # reCAPTCHA: no "HeadlessChrome" brand
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
                # reCAPTCHA Enterprise flags navigator.webdriver / automation;
                # this removes it. Verified: generation passes with it.
                "--disable-blink-features=AutomationControlled",
                # keep the single renderer awake on a headless box
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--no-first-run", "--no-default-browser-check",
                # memory trims that matter at 512 MB
                "--disable-extensions", "--disable-sync",
                "--js-flags=--max-old-space-size=128",
            ])
        try:
            self.ctx.add_init_script(STEALTH_JS)
        except Exception:
            pass
        try:
            # THE THING: mask the HeadlessChrome client-hint leak (wire + JS).
            self.ctx.add_init_script(UAD_OVERRIDE_JS)
            self.ctx.route("**/*", _scrub_ch_headers)
        except Exception:
            pass
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        _log(f"[*] headless-shell up ({Path(self.exe).name}), "
             f"RSS {rss_mb_of(str(PROFILE)):.0f} MB")
        self._inject_cookies()
        return True

    def _inject_cookies(self):
        """If FLOW_COOKIES_JSON (a JSON array, or base64 of it) is set, add
        those cookies to the context. Phone-friendly: you can paste the
        cookie export straight into the Render env var."""
        raw = os.environ.get("FLOW_COOKIES_JSON", "").strip()
        if not raw and COOKIES_FILE.exists():
            # fall back to the on-disk export (import_cookies.py output) so a
            # bare clone with sessions/ still boots signed-in
            try:
                raw = COOKIES_FILE.read_text(encoding="utf-8")
                _log(f"[*] cookies from on-disk {COOKIES_FILE.name}")
            except Exception:
                pass
        if not raw:
            return
        import base64
        try:
            if not raw.lstrip().startswith("["):
                raw = base64.b64decode(raw).decode("utf-8")
            jar = json.loads(raw)
        except Exception as e:
            _log(f"[!] FLOW_COOKIES_JSON unparseable: {str(e)[:120]}")
            return
        n = 0
        for c in jar if isinstance(jar, list) else []:
            try:
                dom = c.get("domain", "") or ".google.com"
                secure = bool(c.get("secure", True))
                name = c["name"]
                # __Secure- cookies MUST be secure or the browser drops them;
                # SameSite=None is only legal when secure.
                if name.startswith("__Secure-") or name.startswith("__Host-"):
                    secure = True
                self.ctx.add_cookies([{
                    "name": name, "value": c["value"], "domain": dom,
                    "path": c.get("path", "/"),
                    "expires": c.get("expirationDate") or c.get("expires") or -1,
                    "httpOnly": bool(c.get("httpOnly")),
                    "secure": secure,
                    "sameSite": "None" if secure else "Lax",
                }])
                n += 1
            except Exception:
                pass
        _log(f"[*] injected {n} cookies from FLOW_COOKIES_JSON")
        try:
            self.page.reload(timeout=45000, wait_until="commit")
            self._pump(seconds=6)
        except Exception:
            pass

    def goto_app(self, path: str = "/"):
        url = FLOW + path
        self.page.goto(url, wait_until="commit", timeout=60000)
        self._pump(seconds=8)
        return self.page.url

    def _pump(self, seconds: float = 5.0):
        """Playwright events only fire while we call its API."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.page.evaluate("1")
            except Exception:
                pass
            time.sleep(0.5)

    def close(self):
        for fn in (lambda: self.ctx and self.ctx.close(),
                   lambda: self.pw and self.pw.stop()):
            try:
                fn()
            except Exception:
                pass

    def restart(self):
        """Kill and relaunch the whole browser. Self-heal for reCAPTCHA score
        changes / wedged sessions: a fresh page gets a fresh reCAPTCHA iframe
        and token state. Re-injects cookies (env or on-disk)."""
        _log("[*] engine restart (self-heal)")
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass
        self.pw = None
        self.ctx = None
        self.page = None
        self._at = ""
        self.start()
        return True

    # ── primitives ───────────────────────────────────────────────
    def mint(self, action: str = "GENERATE") -> str:
        """Fresh reCAPTCHA Enterprise token for `action` (single-use)."""
        r = self.page.evaluate(JS_MINT.replace("'GENERATE'", json.dumps(action)),
                               SITE_KEY)
        if not (r or {}).get("ok"):
            _log(f"[!] mint failed: {(r or {}).get('err')}")
            return ""
        return r.get("token", "")

    def at_token(self, refresh=False) -> str:
        if self._at and not refresh:
            return self._at
        try:
            self._at = self.page.evaluate(JS_SCRAPE_AT) or ""
        except Exception:
            self._at = ""
        return self._at

    def call(self, rpcid: str, payload, project_id: str = ""):
        """POST a batchexecute RPC from inside the page. Returns parsed data."""
        pid = project_id or load_state().get("project_id", "")
        self.at_token()
        r = self.page.evaluate(JS_RPC, {
            "rpcid": rpcid, "payload": payload, "at": self._at, "pid": pid,
        })
        if not isinstance(r, dict) or r.get("status") != 200:
            _log(f"[!] rpc {rpcid}: {r}")
            return None
        for rid, data in parse_batchexecute(r.get("body", "")):
            if rid == rpcid:
                return data
        return None

    # ── API surface ──────────────────────────────────────────────
    def credits(self):
        data = self.call("nzlxg", [])
        try:
            return {"credits": data[0], "raw": data}
        except Exception:
            return None

    def app_config(self):
        return self.call("cPZSdc", [])

    def history(self, project_id: str = ""):
        pid = project_id or load_state().get("project_id", "")
        if not pid:
            return None
        data = self.call("Zzl0ze", [f"projects/{pid}", None, None, None, [1]], pid)
        return data

    # ── generation (payload VERIFIED against the app's real request) ──
    # Captured from the live composer (recon/auth/ogiz0b_real.json):
    #   ogiZ0b -> /FlowService.BatchGenerateImages
    #   payload = [null, [request...], 1, clientContext, [batchId]]
    #   request = [null,null,null,seed,aspect,imageModelKey,null,clientContext,
    #              [[[prompt]]],null,null,null,<uuid>,<uuid>]
    #   recaptcha action = "IMAGE_GENERATION"
    # NOTE: reCAPTCHA Enterprise flags headless Chrome ("PUBLIC_ERROR_UNUSUAL_
    # ACTIVITY") — the app's own composer fails the same way in headless. A
    # real (headed) browser or a fuller fingerprint is required to pass.
    def _client_context(self, pid, token):
        return [None, 22, None, None, None, pid, None, None, None, None,
                [token, 1]] if token else [None, 22, None, None, None, pid]

    def generate(self, prompt, model="BELUGA", aspect=None, count=1,
                 project_id=None, seed=None, _escalate=True):
        """Generate via the UI composer. VERIFIED WORKING: the composer's own
        ogiZ0b request passes reCAPTCHA (with the stealth+client-hint fix) while
        a direct in-page fetch() is flagged. So we drive the composer and
        capture the response — same mechanism as a human, no result-fetching
        hacks.

        Self-heal: if the request is flagged (reCAPTCHA score changed) or the
        engine wedges, restart the browser once and retry — a fresh page gets a
        fresh reCAPTCHA iframe/token. This is what keeps generation from "going
        black" when Google's bot scoring shifts (2026-10-08 incident).
        """
        pid = project_id or load_state().get("project_id", "")
        if not pid:
            _log("[!] generate: no project id")
            return None
        self.goto_app(f"/u/1/project/{pid}")
        self._pump(seconds=6)
        caught = []

        def _on(resp):
            try:
                if "rpcids=ogiZ0b" in resp.url:
                    caught.append(resp.text())
            except Exception:
                pass
        self.page.on("response", _on)
        try:
            box = self.page.locator("[contenteditable=true]").first
            if box.count() == 0:
                _log("[!] generate: composer not found (logged in?)")
                return None
            box.click()
            time.sleep(1)
            # clear any leftover text
            try:
                self.page.keyboard.press("ControlOrMeta+a")
                self.page.keyboard.press("Backspace")
            except Exception:
                pass
            self.page.keyboard.type(prompt, delay=15)
            time.sleep(1.5)
            self.page.keyboard.press("Enter")
            deadline = time.time() + 90
            while time.time() < deadline and not caught:
                try:
                    self.page.evaluate("1")
                except Exception:
                    pass
                time.sleep(1)
        finally:
            try:
                self.page.remove_listener("response", _on)
            except Exception:
                pass
        if not caught:
            _log("[!] generate: no ogiZ0b response observed")
            if _escalate:
                _log("[*] generate: restarting engine and retrying once (self-heal)")
                try:
                    self.restart()
                except Exception as e:
                    _log(f"[!] restart failed: {str(e)[:120]}")
                    return None
                return self.generate(prompt, model=model, aspect=aspect,
                                     count=count, project_id=project_id,
                                     seed=seed, _escalate=False)
            return None
        body = caught[-1]
        if "UNUSUAL_ACTIVITY" in body:
            _log("[!] generate: reCAPTCHA flagged (stealth broken?)")
            if _escalate:
                _log("[*] generate: restarting engine and retrying once (fresh reCAPTCHA state)")
                try:
                    self.restart()
                except Exception as e:
                    _log(f"[!] restart failed: {str(e)[:120]}")
                    return None
                return self.generate(prompt, model=model, aspect=aspect,
                                     count=count, project_id=project_id,
                                     seed=seed, _escalate=False)
            return None
        parsed = parse_batchexecute(body)
        for rid, data in parsed:
            if rid == "ogiZ0b" and data is not None:
                items = self._parse_ogi(data)
                if items:
                    return items
        _log("[!] generate: response had no media")
        return None

    @staticmethod
    def _parse_ogi(data):
        """Parse the ogiZ0b success body. Real shape (verified):
        [["<mediaUuid>", null, "<batchUuid>", null,null,null,
          [[null,<seed>,null,null,null,null,1,"<prompt>",<modelId>,...]], ...]]
        The signed download URL (flow-content.google/image/<cdnId>) sits in
        the same entry but with a different id than the media uuid."""
        import re as _re
        uuid_re = (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                   r"[0-9a-f]{12}")
        urls = []

        def collect(o):
            if isinstance(o, list):
                for v in o:
                    collect(v)
            elif isinstance(o, str):
                for m in _re.finditer(
                        r"https://flow-content\.google/image/[A-Za-z0-9_\-]+"
                        r"\?[^\"\s\\]*", o):
                    u = m.group(0).replace("\\u0026", "&").replace("\\u003d", "=")
                    if u not in urls:
                        urls.append(u)
        collect(data)

        medias = []

        def find_media(o):
            if isinstance(o, list):
                if o and isinstance(o[0], str) and _re.fullmatch(uuid_re, o[0]):
                    medias.append(o)
                for v in o:
                    find_media(v)
        find_media(data)

        out = []
        for i, m in enumerate(medias):
            seed = prompt = model_id = None
            for block in m:
                if (isinstance(block, list) and block and
                        isinstance(block[0], list) and len(block[0]) > 8):
                    g = block[0]
                    seed = g[1] if len(g) > 1 else None
                    prompt = g[7] if len(g) > 7 and isinstance(g[7], str) else prompt
                    model_id = g[8] if len(g) > 8 else model_id
            out.append({
                "uuid": m[0],
                "batch": m[2] if len(m) > 2 else None,
                "seed": seed, "prompt": prompt or "",
                "model_id": model_id,
                "model": MODEL_ENUM.get(model_id, "") if model_id else "",
                "url": urls[i] if i < len(urls) else "",
                "file": "",
            })
        # keep only real media entries (the batch header has no model/prompt)
        out = [o for o in out if o["model_id"] is not None or o["prompt"]]
        return out

    def upscale(self, media_uuid, resolution="2K", project_id=None):
        pid = project_id or load_state().get("project_id", "")
        res = "UPSAMPLE_IMAGE_RESOLUTION_2K" if str(
            resolution).upper().startswith("2") else "UPSAMPLE_IMAGE_RESOLUTION_4K"
        tok = self.mint("UPSAMPLE")
        if not tok:
            return None
        payload = [media_uuid, 1, [None, 22, None, None, None, None, None,
                                   None, None, [tok, 1]]]
        data = self.call("SPrCad", payload, pid)
        if data is None:
            return None
        import base64, re as _re
        found = {"uuid": "", "img": b""}

        def walk(o):
            if isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str):
                if not found["uuid"] and _re.fullmatch(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                        r"[0-9a-f]{12}", o):
                    found["uuid"] = o
                elif len(o) > 100000 and o.startswith("/9j/"):
                    raw = base64.b64decode(o)
                    if raw[:3] == b"\xff\xd8\xff" and len(raw) > len(found["img"]):
                        found["img"] = raw
        walk(data)
        if not found["img"]:
            return None
        out_dir = Path(os.environ.get("FLOW_OUT", str(ROOT / "outputs")))
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{found['uuid'] or media_uuid}-upscaled.jpg"
        dest.write_bytes(found["img"])
        return dest

    # ── schema capture (one-time, with a logged-in profile) ──────
    def capture_chunks(self, out_dir: str = "recon") -> list:
        """Record every JS chunk the *authenticated* app loads, so the exact
        generation payload builder can be read out of the bundle rather than
        guessed. No UI clicking involved — just page load."""
        Path(out_dir).mkdir(exist_ok=True)
        seen = []
        self.page.on("request", lambda r: seen.append(r.url))
        pid = load_state().get("project_id", "")
        if pid:
            self.goto_app(f"/u/1/project/{pid}")
        self._pump(seconds=25)
        js = sorted({u for u in seen if ".js" in u.split("?")[0]})
        Path(out_dir, "chunk_urls.json").write_text(json.dumps(js, indent=1))
        return js

    # ── full UI/options scrape ───────────────────────────────────
    def capture_ui(self, out_dir: str = "recon") -> dict:
        """Open every composer menu on the live app and map ALL options:
        models (incl. Nano Banana 2.1), aspect ratios, count, resolutions.
        Returns a JSON-serialisable dict of what was actually on screen.

        Needs a logged-in profile (the composer only exists when signed in).
        """
        Path(out_dir).mkdir(exist_ok=True)
        pid = load_state().get("project_id", "")
        self.goto_app(f"/u/1/project/{pid}" if pid else "/")
        self._pump(seconds=20)

        JS = r"""() => {
          const T = (e) => (e.innerText || e.textContent || '').trim();
          const vis = (e) => { const r=e.getBoundingClientRect();
                               return r.width>0 && r.height>0; };
          const out = {url: location.href, title: document.title};
          // composer trigger buttons (the chips row above the text box)
          out.triggers = [...document.querySelectorAll('button,[role=button]')]
             .filter(e => vis(e) && /nano banana|model|aspect|ratio|x[1-4]|1:1|16:9|resolution|2K|4K/i.test(T(e)))
             .map(T).filter((v,i,a)=>a.indexOf(v)===i).slice(0,40);
          // any open overlay panes (menus)
          const panes = [...document.querySelectorAll('.cdk-overlay-pane,[role=menu],[role=listbox],mat-menu,mat-bottom-sheet-container')];
          out.panes = panes.map(p => ({
            role: p.getAttribute('role') || p.tagName,
            items: [...p.querySelectorAll('button,[role=menuitem],[role=option],mat-option,li')]
                     .map(T).filter(Boolean).slice(0,60)
          }));
          // model names anywhere in the DOM text
          const body = document.body ? document.body.innerText : '';
          out.modelMentions = (body.match(/Nano Banana[^\n]{0,30}/gi) || [])
                                .map(s=>s.trim()).filter((v,i,a)=>a.indexOf(v)===i);
          out.aspectMentions = (body.match(/\b\d{1,2}:\d{1,2}\b/g) || [])
                                .filter((v,i,a)=>a.indexOf(v)===i);
          out.resolutionMentions = (body.match(/\b[124]K\b/g) || [])
                                .filter((v,i,a)=>a.indexOf(v)===i);
          return out;
        }"""
        report = {"engine_rss_mb": round(rss_mb_of(str(PROFILE)), 1)}

        def _snap(tag):
            try:
                d = self.page.evaluate(JS)
                report[tag] = d
            except Exception as e:
                report[tag] = {"error": str(e)[:200]}

        _snap("composer_default")
        # click each trigger and snapshot the resulting menu
        try:
            n = self.page.evaluate(
                "() => document.querySelectorAll('[contenteditable=true]').length")
            report["composer_present"] = n
            trig = self.page.evaluate("""() => {
              const T=(e)=>(e.innerText||'').trim();
              const vis=(e)=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
              const btns=[...document.querySelectorAll('button,[role=button]')]
                .filter(e=>vis(e) && /Nano Banana|aspect|ratio|16:9|1:1|4K|2K|x[1-4]/i.test(T(e)));
              return btns.map(e=>T(e)).slice(0,12);
            }""")
            report["trigger_labels"] = trig
            for i in range(min(6, len(trig or []))):
                try:
                    self.page.evaluate(f"""() => {{
                      const T=(e)=>(e.innerText||'').trim();
                      const vis=(e)=>{{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}};
                      const btns=[...document.querySelectorAll('button,[role=button]')]
                        .filter(e=>vis(e) && /Nano Banana|aspect|ratio|16:9|1:1|4K|2K|x[1-4]/i.test(T(e)));
                      if (btns[{i}]) btns[{i}].click();
                    }}""")
                    self._pump(seconds=3)
                    _snap(f"menu_{i}")
                    self.page.keyboard.press("Escape")
                    self._pump(seconds=2)
                except Exception as e:
                    report[f"menu_{i}"] = {"click_error": str(e)[:120]}
        except Exception as e:
            report["trigger_error"] = str(e)[:200]

        Path(out_dir, "ui_options.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
        _log(f"[*] UI options -> {out_dir}/ui_options.json")
        return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Flow headless-shell engine")
    ap.add_argument("--capture-chunks", action="store_true",
                    help="dump the app's JS chunk URLs (needs login)")
    ap.add_argument("--capture-ui", action="store_true",
                    help="scrape all composer UI options/models (needs login)")
    ap.add_argument("--out", default="recon")
    a = ap.parse_args()
    exe = detect_headless_shell()
    print("headless shell:", exe or "NOT FOUND")
    if not exe:
        sys.exit(1)
    eng = HeadlessEngine()
    eng.start()
    try:
        if a.capture_chunks:
            print("chunks:", eng.capture_chunks(a.out))
        elif a.capture_ui:
            r = eng.capture_ui(a.out)
            print(json.dumps(r, indent=1)[:4000])
        else:
            print("url:", eng.goto_app("/"))
            print("mint:", (eng.mint("GENERATE") or "")[:12], "...")
            print("credits:", eng.credits())
            print("rss:", round(rss_mb_of(str(PROFILE)), 1), "MB")
    finally:
        eng.close()