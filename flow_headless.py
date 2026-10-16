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
BE_PATH = "/_/AiSandboxAngularFrontend/data/batchexecute"
PROFILE = Path(os.environ.get("FLOW_PROFILE", str(ROOT / "profile-copy")))
# AUTH-CRITICAL (see flow_auth.py): the browser profile must run UNBOUND
# (sessions stay rotation-capable from datacenter IPs). Flag is appended in
# HeadlessEngine.start(); kept here so other launchers can reuse it.
UNBOUND_FEATURES = "--disable-features=EnableBoundSessionCredentials,DeviceBoundSessions"
STATE_FILE = ROOT / "sessions" / "flow_api_state.json"
COOKIES_FILE = ROOT / "sessions" / "flow.cookies.full.json"
MEM_GUARD_MB = int(os.environ.get("FLOW_MEM_GUARD_MB", "420"))

# V8 old-space cap for the renderer. 96 MB: the renderer was measured at
# 221MB RSS on the Flow app; a tighter heap forces more frequent GC and
# trims it. Env knob FLOW_JS_HEAP_MB overrides.
JS_HEAP_MB = int(os.environ.get("FLOW_JS_HEAP_MB", "96"))

# How long to wait for the Flow composer to hydrate before generating.
# Render free's 0.1 CPU can take much longer than a local box on a cold page.
# First load right after login is the slowest case (~2 min observed).
COMPOSER_TIMEOUT = int(os.environ.get("FLOW_COMPOSER_TIMEOUT", "120"))


def _proxy_config():
    """Optional egress proxy for the browser, from FLOW_PROXY_URL.

    reCAPTCHA Enterprise scores the *client IP*: a datacenter IP (Render/AWS)
    can be flagged even with a perfect fingerprint, while a residential IP
    passes. Set FLOW_PROXY_URL=http://user:pass@host:port to route all browser
    traffic through a residential proxy. Returns None when unset."""
    raw = os.environ.get("FLOW_PROXY_URL", "").strip()
    if not raw:
        return None
    from urllib.parse import urlparse
    u = urlparse(raw if "://" in raw else "http://" + raw)
    if not u.hostname:
        return None
    cfg = {"server": f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")}
    if u.username:
        cfg["username"] = u.username
    if u.password:
        cfg["password"] = u.password
    return cfg

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


def read_rss_mb() -> int:
    """RSS of THIS python process (MB)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def total_tree_rss_mb() -> float:
    """python + every chrome/headless child (MB). The REAL container number
    Render's OOM killer sees."""
    total = read_rss_mb()
    for p in glob.glob("/proc/[0-9]*"):
        try:
            cmd = open(p + "/cmdline", "rb").read()
            low = cmd.lower()
            # chrome tree + the playwright node driver (~120MB!) — both live
            # in the container and Render's OOM killer sees them all.
            is_chrome = (b"chrome" in low or b"headless" in low)
            is_driver = b"node" in low and b"playwright" in low
            if is_chrome or is_driver:
                for line in open(p + "/status"):
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1]) / 1024.0
                        break
        except Exception:
            pass
    return total


def proc_breakdown() -> str:
    """Per-kind process count + RSS sum, for diagnosing what makes the
    container fat (chrome children vs node drivers vs python)."""
    kinds = {}
    for p in glob.glob("/proc/[0-9]*"):
        try:
            cmd = open(p + "/cmdline", "rb").read().replace(
                b"\0", b" ").decode("utf-8", "replace")
            low = cmd.lower()
            if "chrome" in low or "headless" in low:
                kind = "chrome"
                # child type helps spot renderer bloat
                for tok in cmd.split():
                    if tok.startswith("--type="):
                        kind = "chrome:" + tok[7:][:9]
                        break
                if kind == "chrome":
                    kind = "chrome:browser"
            elif "node" in low and "playwright" in low:
                kind = "node-driver"
            elif "flow_server.py" in cmd or "python" in low:
                kind = "python"
            else:
                continue
            rss = 0
            for line in open(p + "/status"):
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) // 1024
                    break
            c, t = kinds.get(kind, (0, 0))
            kinds[kind] = (c + 1, t + rss)
        except Exception:
            pass
    return " | ".join(f"{k} x{c}={t}MB" for k, (c, t) in
                      sorted(kinds.items(), key=lambda kv: -kv[1][1]))


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


def save_state(st: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────
# in-page JS: mint + batchexecute (the genuine API calls)
# ────────────────────────────────────────────────────────────────
JS_MINT = """(site) => new Promise((resolve) => {
  const fin = (o) => resolve(o);
  const exec = (g) => {
    try {
      g.ready(() => g.execute(site, {action: 'GENERATE'})
        .then(t => fin({ok: true, token: t}))
        .catch(e => fin({ok: false, err: 'execute: ' + String(e).slice(0, 200)})));
    } catch (e) {
      fin({ok: false, err: 'ready: ' + String(e).slice(0, 200)});
    }
  };
  const go = () => {
    const g = (window.grecaptcha && window.grecaptcha.enterprise)
      || window.grecaptcha;
    if (g) return exec(g);
    return fin({ok: false, err: 'no_grecaptcha'});
  };
  // Flow enforces Trusted Types (require-trusted-types-for 'script'), so a
  // bare `s.src = url` throws:
  //   TypeError: Failed to set the 'src' property on 'HTMLScriptElement':
  //   This document requires 'TrustedScriptURL' assignment.
  const ttURL = (u) => {
    try {
      if (window.trustedTypes && window.trustedTypes.createPolicy) {
        if (!window.__flowTTPolicy) {
          window.__flowTTPolicy = window.trustedTypes.createPolicy(
            'flowMintPolicy', {createScriptURL: (x) => x});
        }
        return window.__flowTTPolicy.createScriptURL(u);
      }
    } catch (e) { /* fall through to the raw URL */ }
    return u;
  };
  const urls = [
    'https://www.google.com/recaptcha/enterprise.js?render=' + site,
    'https://www.google.com/recaptcha/api.js?render=' + site,
  ];
  let ui = 0;
  const inject = () => {
    if (ui >= urls.length) return fin({ok: false, err: 'script_error (all loaders)'});
    const url = urls[ui++];
    try {
      const s = document.createElement('script');
      s.src = ttURL(url);
      s.onload = go;
      // the CSP/network refused this loader — try the next one
      s.onerror = () => inject();
      document.head.appendChild(s);
    } catch (e) {
      fin({ok: false, err: 'inject_failed: ' + String(e).slice(0, 200)});
    }
  };
  if ((window.grecaptcha && window.grecaptcha.enterprise) || window.grecaptcha) {
    return go();
  }
  // Prefer the app's own loader: Flow brings grecaptcha in itself, so wait for
  // it before injecting our own copy. On a 0.1-CPU container the app can take
  // a long while to boot, hence the generous window.
  let waited = 0;
  const wait = setInterval(() => {
    waited += 500;
    if ((window.grecaptcha && window.grecaptcha.enterprise) || window.grecaptcha) {
      clearInterval(wait);
      go();
    } else if (waited >= 40000) {
      clearInterval(wait);
      inject();
    }
  }, 500);
  setTimeout(() => fin({ok: false, err: 'mint_timeout'}), 90000);
})"""

JS_RPC = """async ({rpcid, payload, at, pid, base}) => {
  const fReq = JSON.stringify([[[rpcid, JSON.stringify(payload), null, "generic"]]]);
  const body = new URLSearchParams();
  body.set('f.req', fReq);
  if (at) body.set('at', at);
  const src = pid ? ('/project/' + pid) : '/';
  // Reuse the app's OWN batchexecute URL captured from live traffic so the
  // path + build-label params stay correct as Google changes them. Current
  // real shape (verified 2026-10-10): NO /u/1/ prefix, path is /_/..., plus
  // bl=, f.sid=, hl=, rt=c. Fall back to that known shape.
  let u = (base && base.length)
    ? base
    : location.origin + '/_/AiSandboxAngularFrontend/data/batchexecute?rpcids=' +
      rpcid + '&source-path=' + encodeURIComponent(src) + '&hl=en-GB&rt=c';
  try {
    const url = new URL(u, location.origin);
    url.searchParams.set('rpcids', rpcid);
    url.searchParams.set('source-path', src);
    const r = await fetch(url.toString(), {
      method: 'POST', credentials: 'include',
      headers: {'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'x-same-domain': '1'},
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
        self._be_base = ""            # app's own batchexecute URL (captured, reused)
        self._live_at = ""            # app's own at token (captured from its body)
        self._force_reseed = False    # set by restart(reseed=True)
        self.last_error = ""          # last engine-side failure (surfaced by /health)
        self.mint_error = ""          # optional reCAPTCHA fallback failure

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
        # DRIVER: pure-Python CDP (flow_cdp) — no Playwright, no Node process.
        # The playwright node driver held ~117MB RSS, which alone was the gap
        # between 620MB and the 512MB free-tier limit. FLOW_CDP=0 falls back
        # to Playwright for comparison/debugging.
        if os.environ.get("FLOW_CDP", "1") != "0":
            import flow_cdp
            self.pw = flow_cdp.sync_playwright().start()
        else:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
        # MEM-SPEC launch args (512MB free tier): cap V8 heap, merge renderer
        # into the browser process, prevent per-origin renderer spawning.
        # FLOW_SINGLE_PROCESS=0 drops --single-process (fallback if it hangs).
        args = [
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-software-rasterizer", "--disable-gpu-compositing",
            # MEM (measured on Render): a chrome stack on the Flow app =
            # renderer 211 + browser 143 + utility 87; plus zygotes 78 + gpu 84
            # before these flags. --in-process-gpu and --no-zygote remove the
            # gpu/zygote processes (−162MB measured).
            "--in-process-gpu", "--no-zygote",
            # reCAPTCHA Enterprise flags navigator.webdriver / automation;
            # this removes it. Verified: generation passes with it.
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions", "--disable-plugins",
            "--disable-background-networking",
            # keep the single renderer awake on a headless box
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-breakpad", "--disable-crash-reporter",
            "--disable-hang-monitor", "--disable-sync", "--disable-translate",
            "--metrics-recording-only",
            "--no-first-run", "--no-default-browser-check", "--mute-audio",
            # site-per-process off: no renderer per origin; BackForwardCache
            # off: no bfcache duplicates; audio/media services folded in;
            # spare renderer suppressed.
            "--disable-features=Translate,BackForwardCache,AcceptCHFrame,"
            "MediaRouter,OptimizationHints,"
            "ProcessPerSiteUpToMainFrameThreshold,IsolateOrigins,"
            "site-per-process,AudioServiceOutOfProcess,"
            "GlobalMediaControls,PictureInPicture,"
            "SpareRendererForSitePerProcess,"
            # AUTH-CRITICAL: launch UNBOUND so the session's PSIDTS pair
            # accepts server-side rotation (flow_auth.rotate_psidts).
            # Bound sessions 401 RotateCookies from datacenter IPs.
            "EnableBoundSessionCredentials,DeviceBoundSessions",
            f"--js-flags=--max-old-space-size={JS_HEAP_MB}",
            "--renderer-process-limit=1",
        ]
        # NOTE: --single-process is a no-op in this chrome-headless-shell
        # build (renderer/browser/utility stay separate) — kept behind the
        # env flag only for builds that honour it.
        if os.environ.get("FLOW_SINGLE_PROCESS", "1") != "0":
            args.append("--single-process")
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(PROFILE), executable_path=self.exe, headless=True,
            bypass_csp=True,               # needed for in-page script inject
            user_agent=SPOOF_UA,           # reCAPTCHA: no "HeadlessChrome" brand
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
            proxy=_proxy_config(),         # optional residential egress
            args=args)
        try:
            self.ctx.add_init_script(STEALTH_JS)
        except Exception:
            pass
        try:
            # MEM-SPEC resource blocking (saves 40-80 MB): allow the app's
            # own XHRs + HTML + scripts, abort decorative images/media/fonts
            # EXCEPT the avatar/asset thumbnails the composer needs, and
            # abort ALL third-party hosts (analytics, tracking, telemetry).
            def block_heavy(route):
                r = route.request
                u = r.url
                rt = r.resource_type
                try:
                    if "batchexecute" in u or "/api/" in u:
                        return route.continue_()
                    if any(h in u for h in ("flow.google.com",
                                            "flow-content.google",
                                            "accounts.google.com",
                                            "lh3.googleusercontent.com",
                                            "www.google.com",
                                            "www.gstatic.com")):
                        if rt in ("image", "media", "font", "manifest"):
                            if "/user-avatar/" not in u and "/asb/" not in u:
                                return route.abort()
                        return route.continue_()
                    return route.abort()   # everything third-party: dead weight
                except Exception:
                    try:
                        return route.continue_()
                    except Exception:
                        pass
            self.ctx.route("**/*", block_heavy)
        except Exception:
            pass
        try:
            # THE THING: mask the HeadlessChrome client-hint leak (wire + JS).
            self.ctx.add_init_script(UAD_OVERRIDE_JS)
            self.ctx.route("**/*", _scrub_ch_headers)
        except Exception:
            pass
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

        # Capture the app's own batchexecute URL + `at` token from its traffic
        # so our RPCs copy them verbatim (path, build-label, xsrf) instead of
        # guessing — Google changes these and stale guesses return 400.
        def _grab(r):
            try:
                if "batchexecute" in r.url and not self._be_base:
                    self._be_base = r.url
                    m = re.search(r"(?:^|&)at=([^&]+)", r.post_data or "")
                    if m:
                        self._live_at = urllib.parse.unquote(m.group(1))
            except Exception:
                pass

        try:
            self.page.on("request", _grab)
        except Exception:
            pass
        _log(f"[*] headless-shell up ({Path(self.exe).name}), "
             f"RSS {rss_mb_of(str(PROFILE)):.0f} MB")
        self._inject_cookies()
        return True

    def _inject_cookies(self):
        """If FLOW_COOKIES_JSON (a JSON array, or base64 of it) is set, add
        those cookies to the context. Phone-friendly: you can paste the
        cookie export straight into the Render env var.

        IMPORTANT: only seed ONCE per profile. Google rotates
        `__Secure-1PSIDTS` every few minutes; re-injecting the static export on
        every boot overwrites the browser's *fresh* rotated token with a stale
        one, which makes Google treat the session as invalid (the app then
        redirects to /about). The profile keeps the rotated cookies, so after
        the first seed we leave it alone. Set FLOW_FORCE_COOKIES=1 to override.
        """
        # The marker MUST live outside the profile: PROFILE can be baked into
        # the image (FLOW_PROFILE=/app/profile-copy), so a marker written there
        # survives a deploy while the *cookies* it refers to do not. The next
        # boot then believed it was already seeded and started Chrome with a
        # cookie-less profile -> the app redirected to /about and every RPC
        # returned 401. /tmp is per-container: it persists across supervisor
        # engine rebuilds (so we keep freshly rotated PSIDTS) but resets on
        # every deploy (so we re-seed from the new mint).
        seed_marker = Path(os.environ.get("FLOW_SEED_MARKER",
                                          "/tmp/.flow_cookies_seeded"))
        raw = os.environ.get("FLOW_COOKIES_JSON", "").strip()
        if not raw and COOKIES_FILE.exists():
            # fall back to the on-disk export (import_cookies.py output) so a
            # bare clone with sessions/ still boots signed-in
            try:
                raw = COOKIES_FILE.read_text(encoding="utf-8")
                _log(f"[*] cookies from on-disk {COOKIES_FILE.name}")
            except Exception:
                pass
        # Fingerprint the jar and re-seed whenever it CHANGES. The marker used
        # to be a bare timestamp, so a re-minted jar (e.g. right after a
        # master-token update) was ignored and the browser kept
        # stale, logged-out cookies — the app then redirected to /about.
        import hashlib
        sig = (hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
               if raw else "")
        prev = ""
        if seed_marker.exists():
            try:
                prev = seed_marker.read_text(encoding="utf-8").strip()
            except Exception:
                prev = ""
        force = bool(os.environ.get("FLOW_FORCE_COOKIES")) or bool(
            getattr(self, "_force_reseed", False))
        if prev and prev == sig and not force:
            _log("[*] profile already seeded with this jar; keeping rotated cookies")
            try:
                self.page.reload(timeout=45000, wait_until="commit")
                self._pump(seconds=4)
            except Exception:
                pass
            return
        if prev and prev != sig:
            _log("[*] cookie jar changed -> re-seeding browser profile")
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
        try:
            seed_marker.write_text(sig or str(time.time()), encoding="utf-8")
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

    def restart(self, reseed=False):
        """Kill and relaunch the whole browser. Self-heal for reCAPTCHA score
        changes / wedged sessions: a fresh page gets a fresh reCAPTCHA iframe
        and token state. With reseed=True, also re-inject FLOW_COOKIES_JSON /
        the on-disk export (use when logged out)."""
        _log(f"[*] engine restart (self-heal{' + reseed' if reseed else ''})")
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
        self._be_base = ""
        self._live_at = ""
        self._force_reseed = bool(reseed)
        try:
            self.start()
        finally:
            self._force_reseed = False
        return True

    # ── session guard (self-healing auth) ────────────────────────
    def session_ok(self) -> bool:
        """True iff the account is logged in, proven by an authenticated read
        (nzlxg credits). Loads the app first so cookies/`at` are live."""
        try:
            if "flow.google.com" not in (self.page.url or ""):
                self.goto_app("/")
                self._pump(seconds=6)
        except Exception:
            return False
        try:
            return self.call("nzlxg", []) is not None
        except Exception:
            return False

    def ensure_session(self, reseed=True):
        """Guarantee a logged-in session. Order: re-seed from
        FLOW_COOKIES_JSON / on-disk export; if still logged out and
        FLOW_EMAIL + FLOW_PASSWORD are set, perform a full interactive-style
        sign-in (the account's own flow, no 2FA). Returns bool."""
        if self.session_ok():
            return True
        self.last_error = "logged out"
        if reseed:
            _log("[*] session lost -> re-seeding cookies and rebuilding")
            try:
                self.restart(reseed=True)
            except Exception as e:
                self.last_error = f"reseed restart failed: {str(e)[:120]}"
                return False
            if self.session_ok():
                self.last_error = ""
                return True
        # cookies stale/absent -> try a real sign-in
        email = os.environ.get("FLOW_EMAIL", "").strip()
        pwd = os.environ.get("FLOW_PASSWORD", "").strip()
        if not (email and pwd):
            self.last_error = ("logged out; no FLOW_EMAIL/FLOW_PASSWORD for "
                               "auto-login and cookies are stale")
            return False
        _log("[*] cookies dead -> attempting full sign-in with FLOW_EMAIL")
        try:
            ok = self.login(email, pwd)
        except Exception as e:
            self.last_error = f"login() crashed: {str(e)[:150]}"
            return False
        self.last_error = "" if ok else "sign-in failed (wrong creds / bot check / 2FA?)"
        return ok

    def login(self, email: str, password: str) -> bool:
        """Full Google sign-in by driving the real accounts.google.com UI
        (identifier -> password -> continue). Verified flow (HAR 2026-10-11):
        UEkKwb init -> MI613e email -> B4hajb password -> Flow session works.
        No 2FA on the account => password-only challenge. NOT a raw RPC replay:
        identity endpoints are fingerprinted, so we use the real page."""
        import time as _t
        URL = ("https://accounts.google.com/v3/signin/identifier?"
               "continue=https%3A%2F%2Fflow.google.com%2F"
               "&flowName=GlifWebSignIn&flowEntry=ServiceLogin")
        try:
            self.page.goto(URL, wait_until="commit", timeout=60000)
        except Exception as e:
            self.last_error = f"goto signin failed: {str(e)[:120]}"
            return False
        self._pump(seconds=6)
        try:
            box = self.page.locator("#identifierId")
            box.wait_for(state="visible", timeout=30000)
            box.click()
            self.page.keyboard.type(email, delay=25)
            _t.sleep(1)
            # "Next" button (or Enter)
            try:
                self.page.locator("#identifierNext button").first.click(timeout=8000)
            except Exception:
                self.page.keyboard.press("Enter")
            self._pump(seconds=6)
            pw = self.page.locator("input[type=password]:visible")
            pw.wait_for(state="visible", timeout=30000)
            pw.click()
            self.page.keyboard.type(password, delay=25)
            _t.sleep(1)
            try:
                self.page.locator("#passwordNext button").first.click(timeout=8000)
            except Exception:
                self.page.keyboard.press("Enter")
            # wait for either Flow (success) or an error surface
            deadline = _t.time() + 60
            while _t.time() < deadline:
                self._pump(seconds=3)
                try:
                    u = self.page.url or ""
                except Exception:
                    continue
                if "flow.google.com" in u and "/about" not in u:
                    break
                # dismiss passkey nudge / wizard cards if present
                for label in ("Not now", "Skip", "Cancel"):
                    try:
                        btn = self.page.get_by_role("button", name=label)
                        if btn.count() and btn.first.is_visible():
                            btn.first.click(timeout=2000)
                    except Exception:
                        pass
            ok = self.session_ok()
            if not ok:
                # capture what Google actually showed (wrong pwd? bot check? 2FA?)
                try:
                    dbg = ROOT / "sessions" / "login-debug"
                    dbg.mkdir(parents=True, exist_ok=True)
                    self.page.screenshot(path=str(dbg / "login_fail.png"),
                                         full_page=True)
                    (dbg / "login_fail.html").write_text(self.page.content(),
                                                         encoding="utf-8")
                    (dbg / "login_fail.url.txt").write_text(
                        str(self.page.url), encoding="utf-8")
                    _log(f"[*] login debug saved -> {dbg}")
                except Exception:
                    pass
            if ok:
                # persist the fresh (rotating) cookies so future restarts seed
                # from a LIVE session instead of the stale export
                try:
                    import json as _json
                    jar = self.ctx.cookies()
                    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
                    COOKIES_FILE.write_text(_json.dumps(jar), encoding="utf-8")
                    _log(f"[*] login: dumped {len(jar)} cookies -> {COOKIES_FILE.name}")
                except Exception:
                    pass
            _log(f"[*] login() -> {'OK' if ok else 'FAILED'} (final url: {self.page.url})")
            return ok
        except Exception as e:
            self.last_error = f"login flow error: {str(e)[:150]}"
            return False

    # ── primitives ───────────────────────────────────────────────
    def mint(self, action: str = "GENERATE") -> str:
        """Fresh reCAPTCHA Enterprise token for `action` (single-use)."""
        r = self.page.evaluate(JS_MINT.replace("'GENERATE'", json.dumps(action)),
                               SITE_KEY, timeout=120)
        if not (r or {}).get("ok"):
            # The mint is an OPTIONAL fallback — /generate drives the real
            # composer and flow mints its own token there. Recording it in
            # last_error made a healthy service look broken on the dashboard,
            # so keep it in its own field.
            self.mint_error = str((r or {}).get("err"))
            _log(f"[!] mint failed (optional fallback): {self.mint_error}")
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
        """POST a batchexecute RPC from inside the page. Returns parsed data.
        Uses the app's captured URL/at when available (copy-the-headers)."""
        pid = project_id or load_state().get("project_id", "")
        # A stale `at` shows up as HTTP 401 from batchexecute (not 400). The at
        # lives in page state and goes stale when the app re-inits or the
        # session rotates; a captured _live_at can outlive its usefulness.
        # On 401: drop BOTH cached copies, re-scrape from the page, retry once.
        r = None
        for attempt in (0, 1):
            at = (self._live_at or self.at_token(refresh=(attempt == 1)))
            if not at:
                # No XSRF token means the page hasn't initialised (or the
                # session lapsed). Firing anyway returns a confusing 401 and
                # looks like an auth failure; say what it actually is.
                if attempt == 0:
                    self._live_at = ""
                    self._at = ""
                    try:
                        self._pump(seconds=1.5)
                    except Exception:
                        pass
                    continue
                self.last_error = "no at token"
                _log(f"[!] rpc {rpcid}: no `at` token (page not initialised "
                     f"or session lapsed) — not sending")
                return None
            r = self.page.evaluate(JS_RPC, {
                "rpcid": rpcid, "payload": payload, "at": at, "pid": pid,
                "base": self._be_base,
            })
            if isinstance(r, dict) and r.get("status") == 200:
                break
            if (isinstance(r, dict) and r.get("status") in (401, 403)
                    and attempt == 0):
                _log(f"[*] rpc {rpcid}: {r.get('status')} -> refreshing `at` "
                     f"and retrying")
                self._live_at = ""
                self._at = ""
                try:
                    self._pump(seconds=1.5)
                except Exception:
                    pass
                continue
            break
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

    def create_project(self):
        """Create a new Flow project and return its uuid ('' on failure).
        App's real payload (HAR 2026-10-11):
          ["projects/*", [null, ["<name>"]], [null, 22]]  -> ["<uuid>", [<name>]]
        A bare [] also works (verified live) — we send the named shape and
        fall back to [] if it fails."""
        name = time.strftime("%b %d - %H:%M")
        data = self.call("jHPbke", ["projects/*", [None, [name]], [None, 22]])
        if not (isinstance(data, list) and data and isinstance(data[0], str)):
            data = self.call("jHPbke", [])   # fallback: bare shape, verified
        if isinstance(data, list) and data and isinstance(data[0], str):
            return data[0]
        self.last_error = f"create_project failed: {str(data)[:120]}"
        return ""

    def projects(self, limit: int = 30):
        """List projects (rpcid `UpteDb`, payload `["projects/*",21,...,[1]]`).
        VERIFIED 2026-10-10: response is `[[[uuid,[name,null,[ts],thumb,media]]]]`."""
        data = self.call("UpteDb", ["projects/*", 21, None, None, None, None, [1]])
        out = []
        try:
            for rec in (data or []):
                if isinstance(rec, list) and rec and isinstance(rec[0], str):
                    meta = rec[1] if len(rec) > 1 and isinstance(rec[1], list) else []
                    out.append({"project": rec[0],
                                "name": meta[0] if meta else "",
                                "thumb": meta[3] if len(meta) > 3 else "",
                                "media": meta[4] if len(meta) > 4 else ""})
                    if len(out) >= limit:
                        break
        except Exception:
            pass
        return out

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
        # MEM/SELF-HEAL FIX: never restart the engine mid-generation — the
        # caller's page dies with 'Target closed' and the job crashes.
        # session_ok() can false-negative (slow nzlxg on 0.1 CPU); if it
        # fails, just log and CONTINUE — the composer drive below surfaces
        # the real error. ensure_session's browser restart is only safe
        # between jobs (supervisor path), never inside one.
        if not self.session_ok():
            _log("[*] generate: session check failed — continuing without "
                 "restart (mid-job restart kills the page)")
            self.last_error = ""
        pid = project_id or load_state().get("project_id", "")
        if not pid:
            # auto-create a project so generation "just works" with no id set
            _log("[*] generate: no project id -> creating a new project")
            pid = self.create_project()
            if pid:
                try:
                    st = load_state()
                    st["project_id"] = pid
                    save_state(st)
                except Exception:
                    pass
        if not pid:
            _log("[!] generate: no project id")
            return None
        self.goto_app(f"/project/{pid}")
        # 404? the stored project id is stale/deleted -> auto-create a fresh one
        try:
            self._pump(seconds=3)
            if "404" in (self.page.url or ""):
                _log(f"[*] generate: project {pid} is 404 -> creating a new one")
                pid = self.create_project()
                if not pid:
                    self.last_error = "project 404 and auto-create failed"
                    return None
                try:
                    st = load_state()
                    st["project_id"] = pid
                    save_state(st)
                except Exception:
                    pass
                self.goto_app(f"/project/{pid}")
        except Exception:
            pass
        # Poll for the composer instead of a fixed sleep: Angular hydration is
        # slow on Render free's 0.1 CPU (and on a cold project page), so a 6 s
        # sleep often finds an empty DOM and wrongly reports "not logged in".
        # First load after a fresh login is the slow case (up to ~2 min).
        _deadline = time.time() + COMPOSER_TIMEOUT
        while time.time() < _deadline:
            try:
                # visible + enabled, not merely present in the DOM
                _ready = self.page.wait_for_function(
                    """() => {
                      const el = document.querySelector('[contenteditable="true"]');
                      if (!el) return false;
                      const r = el.getBoundingClientRect();
                      if (r.width <= 0 || r.height <= 0) return false;
                      return !el.hasAttribute('disabled') &&
                             el.getAttribute('aria-disabled') !== 'true';
                    }""",
                    timeout=8000)
                if _ready:
                    break
            except Exception:
                pass
            self._pump(seconds=0.5)
        self._pump(seconds=2)
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
                self.last_error = ("composer not found — not logged in? "
                                   "(seed FLOW_COOKIES_JSON / PROFILE_TAR_URL)")
                _log(f"[!] generate: {self.last_error}")
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
            self.last_error = "no ogiZ0b response observed (page slow/wedged?)"
            _log(f"[!] generate: {self.last_error}")
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
            self.last_error = ("reCAPTCHA flagged PUBLIC_ERROR_UNUSUAL_ACTIVITY "
                               "(client IP / fingerprint scored low)")
            _log(f"[!] generate: {self.last_error}")
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
                    self.last_error = ""
                    return items
        # An ogiZ0b response arrived but carried no media. That is usually a
        # transient (Google-side throttle, or two generations racing), and a
        # fresh page normally clears it — so retry once, and log enough of the
        # body to tell an error frame from an empty success.
        self.last_error = "ogiZ0b response had no media"
        _log(f"[!] generate: {self.last_error}; body[:300]="
             f"{str(body)[:300]!r} rpcids={[p[0] for p in parsed][:6]}")
        if _escalate:
            _log("[*] generate: retrying once on a fresh page (no-media heal)")
            try:
                self.restart()
            except Exception as e:
                _log(f"[!] restart failed: {str(e)[:120]}")
                return None
            return self.generate(prompt, model=model, aspect=aspect,
                                 count=count, project_id=project_id,
                                 seed=seed, _escalate=False)
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
        self.goto_app(f"/project/{pid}" if pid else "/")
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
          const body = document.body ? (document.body.textContent || document.body.innerText || '') : '';
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
              const T=(e)=>((e.textContent||e.innerText||'')).trim();
              const vis=(e)=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
              const btns=[...document.querySelectorAll('button,[role=button]')]
                .filter(e=>vis(e) && /Nano Banana|aspect|ratio|16:9|1:1|4K|2K|x[1-4]/i.test(T(e)));
              return btns.map(e=>T(e)).slice(0,12);
            }""")
            report["trigger_labels"] = trig
            for i in range(min(6, len(trig or []))):
                try:
                    self.page.evaluate(f"""() => {{
                      const T=(e)=>((e.textContent||e.innerText||'')).trim();
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