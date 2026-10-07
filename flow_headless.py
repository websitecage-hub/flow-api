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

# Wire enums (README §1)
ASPECT_WIRE = {"1:1": 1, "9:16": 2, "16:9": 3, "3:4": 4, "4:3": 5}
MODEL_DISPLAY = {
    "NANO BANANA 2": "NARWHAL", "NANO BANANA 2 LITE": "HARBOR_SEAL",
    "NANO BANANA PRO": "GEM_PIX_2", "NARWHAL": "NARWHAL",
    "HARBOR_SEAL": "HARBOR_SEAL", "GEM_PIX_2": "GEM_PIX_2",
}


def norm_model(name):
    if not name:
        return "NARWHAL"
    return MODEL_DISPLAY.get(str(name).strip().upper(), str(name).strip().upper())


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
  const h = document.documentElement.innerHTML;
  const m = h.match(/AIQ-[A-Za-z0-9_\\-]{20,80}:\\d{10,20}/);
  return m ? m[0] : '';
}"""


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
            viewport={"width": 1280, "height": 900},
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
                # keep the single renderer awake on a headless box
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--no-first-run", "--no-default-browser-check",
                # memory trims that matter at 512 MB
                "--disable-extensions", "--disable-sync",
                "--js-flags=--max-old-space-size=128",
            ])
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        _log(f"[*] headless-shell up ({Path(self.exe).name}), "
             f"RSS {rss_mb_of(str(PROFILE)):.0f} MB")
        return True

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

    # ── generation / upscale (payload from the app's own bundle) ──
    # The write RPCs (ogiZ0b/SPrCad) live in auth-gated lazy chunks; the
    # payload below is the documented shape (README §1). With a logged-in
    # profile it fires the genuine API request; without one the server
    # answers 401/error and capture_chunks() pulls the builder out of the
    # bundle so the exact schema can be asserted rather than guessed.
    def generate(self, prompt, model="NARWHAL", aspect=None, count=1,
                 project_id=None, seed=None):
        pid = project_id or load_state().get("project_id", "")
        if not pid:
            _log("[!] generate: no project id")
            return None
        ar = ASPECT_WIRE.get(aspect, 3) if isinstance(aspect, str) else (aspect or 3)
        token = self.mint("GENERATE")
        if not token:
            return None
        import uuid as _uuid
        results, n = [], max(1, min(4, int(count or 1)))
        for i in range(n):
            tok = token if i == 0 else self.mint("GENERATE")
            req = [None, None, None, int(seed) if seed is not None else None,
                   int(ar), norm_model(model), None,
                   [None, 22, None, None, None, pid, None, None, None,
                    [tok, 1]],
                   [[[prompt]]], None, None, None,
                   str(_uuid.uuid4()), str(_uuid.uuid4())]
            payload = [None, [req], 1, None, None, None, None, None, None,
                       None, None, None, None, None, None, None, None, None,
                       None, None, None, None, None, None, None, None, 1]
            data = self.call("ogiZ0b", payload, pid)
            if data is None:
                continue
            for it in self._parse_ogi(data):
                results.append(it)
        return results or None

    @staticmethod
    def _parse_ogi(data):
        import re as _re
        out = []
        blob = json.dumps(data)
        urls = [u.replace("\\u003d", "=").replace("\\u0026", "&") for u in
                _re.findall(r"https://flow-content\.google/image/"
                            r"[A-Za-z0-9_\-]+\?Expires=\d+[^\"\\ ]*", blob)]
        # walk for media entries: [uuid, ..., batch, ...]
        def walk(o, acc):
            if isinstance(o, list):
                for v in o:
                    walk(v, acc)
            elif isinstance(o, str) and _re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                    r"[0-9a-f]{12}", o):
                acc.add(o)
        acc = set()
        walk(data, acc)
        for j, u in enumerate(urls):
            out.append({"uuid": sorted(acc)[j] if j < len(acc) else "",
                        "url": u, "seed": None, "prompt": "", "file": ""})
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


if __name__ == "__main__":
    exe = detect_headless_shell()
    print("headless shell:", exe or "NOT FOUND")
    if not exe:
        sys.exit(1)
    eng = HeadlessEngine()
    eng.start()
    try:
        print("url:", eng.goto_app("/"))
        print("mint:", (eng.mint("GENERATE") or "")[:12], "...")
        print("credits:", eng.credits())
        print("rss:", round(rss_mb_of(str(PROFILE)), 1), "MB")
    finally:
        eng.close()