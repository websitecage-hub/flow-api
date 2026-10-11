# -*- coding: utf-8 -*-
"""flow_auth.py — pure-Python Google auth stack for the Flow API.

Three verified breakthroughs (live 2026-10-11, see FLOW-INTERNALS.md §3.4-3.5):

A) __Secure-1PSIDTS rotation works from ANY IP via
   POST https://accounts.google.com/RotateCookies
   — provided the seed session was created UNBOUND
     (Chrome launched with --disable-features=EnableBoundSessionCredentials,
      DeviceBoundSessions — which is exactly how the engine's profile runs).

B) The batchexecute `at` (XSRF) token is disclosed by Google itself: send an RPC
   WITHOUT `at` and the 400 body contains
   [{"48448350":["xsrf","<TOKEN>:<ms>",...]}]  — regex `"xsrf","([^"]+)"`.

C) The whole cookie jar is mintable from a durable master token (aas_et/…):
   gpsoauth.perform_oauth(email, master, android_id,
                          service="oauth2:https://www.google.com/accounts/OAuthLogin",
                          app="com.google.android.apps.chromecast.app",
                          client_sig="24bb24c05e47e0aefa68a58a766179d9b613a600")
   → ya29 bearer → OAuthLogin?issueuberauth=1 → uberauth
   → MergeSession?…&uberauth=<uberauth> → full jar (SID/HSID/SSID/APISID/
     SAPISID/__Secure-1PSID/__Secure-3PSID/LSID/__Host-*/SIDCC/…).
   app MUST be the chromecast app; the gms variant yields cookies that fail
   RotateCookies with 401.

No browser, no CDP, no DBSC proof. curl_cffi for TLS-JA3 realism.
"""
from __future__ import annotations

import os
import re
import time
import json
import threading
from typing import Optional
from urllib.parse import quote as _quote, quote

try:
    from curl_cffi import requests as cffi
except Exception:            # pragma: no cover - fallback if curl_cffi missing
    import requests as cffi  # type: ignore

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

CHROMECAST_APP = "com.google.android.apps.chromecast.app"
CHROMECAST_SIG = "24bb24c05e47e0aefa68a58a766179d9b613a600"

FLOW_ORIGIN = "https://flow.google.com"
BE_PATH = "/_/AiSandboxAngularFrontend/data/batchexecute"

# session-wide switch: launch the engine's Chrome UNBOUND so rotations work
UNBOUND_FEATURES = "--disable-features=EnableBoundSessionCredentials,DeviceBoundSessions"


_SHARE_SESS: Optional["cffi.Session"] = None


def _session() -> "cffi.Session":
    """SHARED curl_cffi session (chrome131 impersonation allocates ~50-80 MB
    of TLS/BoringSSL state — creating one per rotate/harvest call OOM-kills
    a 512 MB container within a few supervisor passes)."""
    global _SHARE_SESS
    if _SHARE_SESS is None:
        try:
            _SHARE_SESS = cffi.Session(impersonate="chrome131")
        except Exception:
            _SHARE_SESS = cffi.Session()
    return _SHARE_SESS


# ─────────────────────────────────────────────────────────────────
# Breakthrough C — mint a full cookie jar from the master token
# ─────────────────────────────────────────────────────────────────
def mint_cookies(email: str, master_token: str, android_id: str = "0123456789abcdef") -> dict:
    """Master token (aas_et/…) → full Google cookie jar (dict name→value).

    Chain: gpsoauth → ya29 → OAuthLogin?issueuberauth=1 → uberauth
           → MergeSession → cookies.
    """
    import gpsoauth
    res = gpsoauth.perform_oauth(
        email, master_token, android_id,
        service="oauth2:https://www.google.com/accounts/OAuthLogin",
        app=CHROMECAST_APP, client_sig=CHROMECAST_SIG,
    )
    ya29 = (res or {}).get("Auth") or ""
    if not ya29.startswith("ya29"):
        # NEVER include res/ya29/token in the message — secrets must not leak
        # into logs. Keys only.
        raise RuntimeError(f"gpsoauth failed: keys={sorted((res or {}).keys())}")

    s = _session()
    r = s.get(
        "https://accounts.google.com/OAuthLogin",
        params={"source": "ChromiumBrowser", "issueuberauth": "1"},
        headers={"Authorization": f"Bearer {ya29}", "User-Agent": UA},
        timeout=60,
    )
    r.raise_for_status()
    uberauth = r.text.strip()
    if not uberauth:
        raise RuntimeError("empty uberauth")

    r = s.get(
        "https://accounts.google.com/MergeSession",
        params={"service": "mail",
                "continue": "https://www.google.com",
                "uberauth": uberauth},
        headers={"Authorization": f"Bearer {ya29}", "User-Agent": UA},
        timeout=60,
    )
    r.raise_for_status()

    jar = {}
    for c in s.cookies.jar:
        jar[c.name] = c.value
    need = ("SID", "HSID", "SSID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID")
    missing = [n for n in need if n not in jar]
    if missing:
        raise RuntimeError(f"mint incomplete, missing {missing}; got {sorted(jar)[:12]}")
    return jar


# ─────────────────────────────────────────────────────────────────
# Breakthrough A — rotate __Secure-{1,3}PSIDTS from any IP
# ─────────────────────────────────────────────────────────────────
def rotate_psidts(jar: dict, session: Optional["cffi.Session"] = None):
    """Rotate the PSIDTS pair. jar is mutated in place when refreshed.

    Returns (http_status, refreshed_bool). 200 + new sidts values = rotated.
    The seed session must be UNBOUND (see UNBOUND_FEATURES) for this to work
    from datacenter IPs.
    """
    s = session or _session()
    for name, value in jar.items():
        s.cookies.set(name, value, domain=".google.com")
    r = s.post(
        "https://accounts.google.com/RotateCookies",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://accounts.google.com",
            "Referer": "https://accounts.google.com/",
            "User-Agent": UA,
        },
        data='[000,"-0000000000000000000"]',
        timeout=60,
    )
    refreshed = False
    if r.status_code == 200:
        for c in s.cookies.jar:
            if c.name in ("__Secure-1PSIDTS", "__Secure-3PSIDTS"):
                old = jar.get(c.name)
                if c.value and c.value != old:
                    jar[c.name] = c.value
                    refreshed = True
    return r.status_code, refreshed


# ─────────────────────────────────────────────────────────────────
# Breakthrough B — harvest the `at` token (Google discloses it)
# ─────────────────────────────────────────────────────────────────
_XSRF_RE = re.compile(r'"xsrf","([^"]+)"')


def harvest_at(session: "cffi.Session", bl: str, fsid: str) -> str:
    """Send an RPC without `at`; parse the disclosed token from the 400 body."""
    url = (FLOW_ORIGIN + BE_PATH +
           "?rpcids=o30O0e&source-path=%2F"
           f"&bl={bl}&f.sid={fsid}&rt=c")
    body = 'f.req=' + _freq([["o30O0e", "[]", None, "generic"]])
    r = session.post(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                 "X-Same-Domain": "1", "User-Agent": UA,
                 "Origin": FLOW_ORIGIN, "Referer": FLOW_ORIGIN + "/"},
        timeout=60,
    )
    m = _XSRF_RE.search(r.text)
    if not m:
        raise RuntimeError(f"no xsrf in {r.status_code} body: {r.text[:200]}")
    return m.group(1)


def extract_wiz(session: "cffi.Session") -> tuple[str, str]:
    """Parse window.WIZ_global_data from https://flow.google.com/ → (bl, fsid)."""
    r = session.get(FLOW_ORIGIN + "/", headers={"User-Agent": UA}, timeout=60)
    html = r.text
    bl = fsid = ""
    m = re.search(r'"FdrFJe":"(-?\d+)"', html)      # f.sid
    if m:
        fsid = m.group(1)
    m = re.search(r'"cfb2h":"([^"]+)"', html)       # bl (build label)
    if m:
        bl = m.group(1)
    if not (bl and fsid):
        # HTML may omit them; probe a 400 which always echoes both
        r2 = session.post(
            FLOW_ORIGIN + BE_PATH + "?rpcids=o30O0e&source-path=%2F&rt=c",
            data='f.req=' + _freq([["o30O0e", "[]", None, "generic"]]),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                     "User-Agent": UA, "Origin": FLOW_ORIGIN},
            timeout=60)
        m = re.search(r'"([0-9]{9,12})"\s*,\s*"xsrf"', r2.text)
        if r2.status_code == 400:
            # fallback: any 400 lists the missing-param details incl. bl/fsid hints
            j = re.search(r'"ds:VjqWWe","([^"]+)"', r2.text)
            if j:
                bl = bl or j.group(1)
    return bl, fsid


def _freq(chunks) -> str:
    """URLSearchParams-style encoding of f.req (matches the browser)."""
    return _quote(json.dumps(chunks), safe="")


# ─────────────────────────────────────────────────────────────────
# Authenticated batchexecute from pure HTTP (reads + light writes)
# ─────────────────────────────────────────────────────────────────
def rpc(session: "cffi.Session", rpcid: str, payload, at: str,
        bl: str, fsid: str, source_path: str = "/"):
    """One batchexecute call with cookies + at. Returns (status, parsed)."""
    url = (FLOW_ORIGIN + BE_PATH +
           f"?rpcids={rpcid}&source-path={_quote(source_path, safe='')}"
           f"&bl={bl}&f.sid={fsid}&rt=c")
    body = ("f.req=" + _freq([[rpcid, json.dumps(payload), None, "generic"]])
            + "&at=" + _quote(at, safe=""))
    r = session.post(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                 "X-Same-Domain": "1", "User-Agent": UA,
                 "Origin": FLOW_ORIGIN, "Referer": FLOW_ORIGIN + "/"},
        timeout=60,
    )
    if r.status_code != 200:
        return r.status_code, None
    # parse wrb.fr framing
    for line in r.text.split("\n"):
        line = line.strip()
        if not line.startswith('[["wrb.fr"'):
            continue
        try:
            env = json.loads(line)
        except Exception:
            continue
        for chunk in env:
            if isinstance(chunk, list) and len(chunk) > 2 and chunk[0] == "wrb.fr":
                if chunk[1] == rpcid:
                    try:
                        return 200, json.loads(chunk[2]) if chunk[2] else None
                    except Exception:
                        return 200, chunk[2]
    return 200, None


# ─────────────────────────────────────────────────────────────────
# KV persistence (Cloudflare Worker) — same pattern as the DS bridge
# ─────────────────────────────────────────────────────────────────
def kv_get() -> Optional[dict]:
    url, secret = os.environ.get("FLOW_KV_URL", ""), os.environ.get("FLOW_KV_SECRET", "")
    if not url:
        return None
    try:
        r = _session().get(url, headers={"Authorization": secret} if secret else {},
                           timeout=30)
        if r.status_code != 200:
            return None
        d = r.json()
        return d.get("jar") or d.get("value") or d
    except Exception:
        return None


def kv_put(jar: dict, extra: Optional[dict] = None) -> bool:
    url, secret = os.environ.get("FLOW_KV_URL", ""), os.environ.get("FLOW_KV_SECRET", "")
    if not url:
        return False
    try:
        payload = {"jar": jar, "ts": time.time()}
        if extra:
            payload.update(extra)
        r = _session().put(url, json=payload,
                           headers={"Authorization": secret} if secret else {},
                           timeout=30)
        return r.status_code in (200, 204)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────
# Background rotation loop
# ─────────────────────────────────────────────────────────────────
class RotationLoop(threading.Thread):
    """Every FLOW_ROTATE_S (default 600): rotate PSIDTS, persist to KV."""

    def __init__(self, get_jar, set_jar, interval=600, on_rotate=None):
        super().__init__(daemon=True, name="psidts-rotation")
        self.get_jar = get_jar
        self.set_jar = set_jar
        self.interval = int(os.environ.get("FLOW_ROTATE_S", interval))
        self.on_rotate = on_rotate or (lambda ok, code: None)
        self.last_ok = 0.0
        self.last_status = 0

    def rotate_now(self):
        jar = self.get_jar()
        if not jar:
            return False
        try:
            code, ok = rotate_psidts(jar)
        except Exception:
            return False
        self.last_status = code
        if ok:
            self.set_jar(jar)
            self.last_ok = time.time()
            self.on_rotate(True, code)
        else:
            self.on_rotate(False, code)
        return ok

    def run(self):
        while True:
            time.sleep(self.interval)
            try:
                self.rotate_now()
            except Exception:
                pass
