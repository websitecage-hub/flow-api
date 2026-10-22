"""flow_cdp.py — pure-Python Chrome DevTools Protocol driver.

Replaces Playwright for this project. WHY: Playwright spawns a Node.js
"driver" process that holds ~117-120 MB RSS for the lifetime of the browser.
On Render's free tier (512 MB) that is the entire gap between 620 MB and the
limit. This module talks CDP over a WebSocket directly from Python:

    python + websocket-client (~5 MB)   instead of   node driver (117 MB)

It exposes the small Playwright-compatible surface the engine uses:
    pw.chromium.launch_persistent_context(...)
    ctx.pages / new_page() / add_init_script() / route() / add_cookies()
    ctx.cookies() / close()
    page.goto / reload / url / evaluate / wait_for_function / content /
         screenshot / locator(...).first.{count,click,wait_for} /
         get_by_role(...).first.click / keyboard.type / keyboard.press /
         on("request"|"response") / remove_listener

NOTES / deliberate divergences:
  * route() handlers are CHAINED (all handlers see every request; any may
    abort; header rewrites from all of them are merged). Playwright's
    "last handler wins" silently skipped our resource-blocking handler.
  * No Node process is spawned anywhere. No Playwright import.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional


class CdpError(RuntimeError):
    pass


# ────────────────────────────────────────────────────────────────
# transport: one WebSocket, a reader thread, id-correlated requests
# ────────────────────────────────────────────────────────────────
class _Conn:
    def __init__(self, ws_url: str, timeout: float = 30.0):
        import websocket  # websocket-client (pure python)
        self._ws = websocket.create_connection(
            ws_url, timeout=timeout, max_size=None,
            enable_multithread=True, suppress_origin=True)
        self._lock = threading.Lock()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._next_id = 1
        self._handlers: Dict[str, List[Callable]] = {}
        self._hlock = threading.Lock()
        self._closed = False
        self._events: "queue.Queue" = queue.Queue()
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()
        self._d = threading.Thread(target=self._dispatcher, daemon=True)
        self._d.start()

    # -- reader: NEVER runs user handlers (they may send CDP commands, which
    #    would need this thread to read the reply -> deadlock). It only
    #    routes id-replies and queues events.
    def _reader(self):
        while not self._closed:
            try:
                raw = self._ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None:
                with self._lock:
                    slot = self._pending.pop(mid, None)
                if slot:
                    slot["result"] = msg.get("result")
                    slot["error"] = msg.get("error")
                    slot["ev"].set()
            else:
                method = msg.get("method")
                if method:
                    self._events.put((method, msg.get("params") or {},
                                      msg.get("sessionId")))

    def _dispatcher(self):
        while not self._closed:
            try:
                method, params, sid = self._events.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._hlock:
                handlers = list(self._handlers.get(method, ()))
            for h in handlers:
                try:
                    h(params, sid)
                except Exception:
                    pass

    # -- api ----------------------------------------------------
    def on(self, method: str, handler: Callable):
        with self._hlock:
            self._handlers.setdefault(method, []).append(handler)

    def off(self, method: str, handler: Callable):
        with self._hlock:
            lst = self._handlers.get(method) or []
            if handler in lst:
                lst.remove(handler)

    def send(self, method: str, params: Optional[dict] = None,
             session_id: Optional[str] = None, timeout: float = 30.0,
             wait: bool = True):
        with self._lock:
            mid = self._next_id
            self._next_id += 1
        payload: Dict[str, Any] = {"id": mid, "method": method,
                                   "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        ev = threading.Event()
        slot = {"ev": ev, "result": None, "error": None}
        with self._lock:
            self._pending[mid] = slot
        try:
            self._ws.send(json.dumps(payload))
        except Exception as e:
            with self._lock:
                self._pending.pop(mid, None)
            raise CdpError(f"send {method}: {e}") from e
        if not wait:
            return None
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise CdpError(f"{method}: timeout after {timeout}s")
        if slot["error"]:
            raise CdpError(f"{method}: {slot['error'].get('message')}")
        return slot["result"]

    def close(self):
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────
# route / request / response shims (Playwright-shaped)
# ────────────────────────────────────────────────────────────────
class _Chain:
    """Collects a decision across ALL route handlers for one request."""

    def __init__(self):
        self.abort = False
        self.headers: Optional[dict] = None
        self.continued = False


class Request:
    def __init__(self, url, method="GET", headers=None, resource_type="other",
                 post_data=""):
        self.url = url
        self.method = method
        self.headers = headers or {}
        self.resource_type = (resource_type or "other").lower()
        self.post_data = post_data or ""


class Route:
    def __init__(self, chain: _Chain, request: Request):
        self._chain = chain
        self.request = request

    def continue_(self, headers=None, **_kw):
        self._chain.continued = True
        if headers:
            self._chain.headers = headers

    def abort(self, **_kw):
        self._chain.abort = True
        self._chain.continued = True

    def fallback(self, **_kw):
        pass


class Response:
    """Lazy response body: CDP only fetches the body on demand."""

    def __init__(self, page: "Page", url: str, request_id: str,
                 mime: str = ""):
        self.url = url
        self.request_id = request_id
        self.mime_type = mime
        self.status = 0
        self._page = page
        self._text: Optional[str] = None

    def text(self) -> str:
        if self._text is None:
            try:
                r = self._page._conn.send(
                    "Network.getResponseBody",
                    {"requestId": self.request_id},
                    session_id=self._page._sid, timeout=30)
                body = r.get("body", "")
                self._text = (base64.b64decode(body).decode("utf-8", "replace")
                              if r.get("base64Encoded") else body)
            except Exception:
                self._text = ""
        return self._text


# ────────────────────────────────────────────────────────────────
# locator
# ────────────────────────────────────────────────────────────────
def _js_escape(s: str) -> str:
    return json.dumps(s)


# headers CDP refuses to set via Fetch.continueRequest (attempting them makes
# the call fail, which would leave the request paused forever)
_FORBIDDEN = {"host", "content-length", "connection", "transfer-encoding",
              "keep-alive", "upgrade", "proxy-connection", "proxy-authorization",
              "te", "trailer"}


def _clean_headers(h: dict) -> List[dict]:
    out = []
    for k, v in (h or {}).items():
        lk = str(k).lower()
        if not lk or lk.startswith(":") or lk in _FORBIDDEN:
            continue
        out.append({"name": str(k), "value": str(v)})
    return out


class Locator:
    def __init__(self, page: "Page", selector: str = "", role: str = "",
                 name: str = "", first: bool = False):
        self._page = page
        self._sel = selector
        self._role = role
        self._name = name
        self._first = first

    # -- construction -------------------------------------------
    @property
    def first(self) -> "Locator":
        return Locator(self._page, self._sel, self._role, self._name, True)

    # -- js to find elements ------------------------------------
    def _find_js(self) -> str:
        if self._role:
            role, name = self._role, self._name or ""
            return (
                "(() => {const out=[];const want=%s;const r=%s;"
                "const els=document.querySelectorAll("
                "'button,[role=button],a,[role=link],input,[role=textbox]');"
                "for (const e of els) {const rl=(e.getAttribute('role')||"
                "e.tagName.toLowerCase());const t=(e.innerText||e.value||'').trim();"
                "if ((rl===r||(r==='button'&&e.tagName==='BUTTON')) && "
                "(!want||t.includes(want))) out.push(e);}return out;})()"
                % (_js_escape(name), _js_escape(role)))
        return (f"Array.from(document.querySelectorAll({_js_escape(self._sel)}))")

    def is_visible(self) -> bool:
        js = (self._find_js() + "[0]") if self._role else \
             f"document.querySelector({_js_escape(self._sel)})"
        try:
            v = self._page.evaluate(
                f"(() => {{const e={js}; if(!e) return false;"
                "const r=e.getBoundingClientRect();"
                "const s=getComputedStyle(e);"
                "return !!(r.width>0&&r.height>0&&s.visibility!=='hidden'"
                "&&s.display!=='none');})()")
            return bool(v)
        except Exception:
            return False

    def count(self) -> int:
        try:
            if self._role:
                js = self._find_js() + ".length"
                n = self._page.evaluate(js) or 0
            else:
                n = self._page.evaluate(
                    f"document.querySelectorAll({_js_escape(self._sel)}).length"
                ) or 0
            return 1 if (self._first and n) else int(n)
        except Exception:
            return 0

    def wait_for(self, state: str = "visible", timeout: float = 30000):
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            if self.count():
                if state in ("attached", "visible"):
                    return True
            time.sleep(0.4)
        raise CdpError(f"locator wait_for({state}) timed out")

    def bounding_box(self):
        js = (self._find_js() + "[0]") if self._role else \
             f"document.querySelector({_js_escape(self._sel)})"
        return self._page.evaluate(
            f"(() => {{const e={js}; if(!e) return null;"
            "const r=e.getBoundingClientRect();"
            "return {x:r.x,y:r.y,w:r.width,h:r.height};})()")

    def click(self, timeout: float = 30000, **_kw):
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            box = self.bounding_box()
            if box and box.get("w", 0) > 0 and box.get("h", 0) > 0:
                cx = box["x"] + box["w"] / 2.0
                cy = box["y"] + box["h"] / 2.0
                # focus first: the composer only accepts keystrokes when the
                # element (not just the page) holds focus
                try:
                    js = (self._find_js() + "[0]") if self._role else \
                         f"document.querySelector({_js_escape(self._sel)})"
                    self._page.evaluate(f"(() => {{const e={js};"
                                        "if(e && e.focus) e.focus(); return true;})()")
                except Exception:
                    pass
                self._page.mouse_click(cx, cy)
                return True
            time.sleep(0.4)
        raise CdpError("locator click timed out (not visible)")

    def fill(self, text: str, **_kw):
        self.click()
        time.sleep(0.2)
        self._page.keyboard.press("ControlOrMeta+a")
        self._page.keyboard.type(text)


# ────────────────────────────────────────────────────────────────
# page
# ────────────────────────────────────────────────────────────────
_KEYMAP = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "escape": ("Escape", "Escape", 27, ""),
    "tab": ("Tab", "Tab", 9, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "delete": ("Delete", "Delete", 46, ""),
}


class Keyboard:
    def __init__(self, page: "Page"):
        self._p = page

    def press(self, key: str):
        raw = key or ""
        mods = 0
        target = raw
        if "+" in raw:
            parts = raw.split("+")
            target = parts[-1]
            for m in parts[:-1]:
                ml = m.lower()
                if ml in ("control", "ctrl", "controlormeta", "meta"):
                    mods |= 2 if ml in ("control", "ctrl", "controlormeta") else 4
                elif ml == "shift":
                    mods |= 8
                elif ml == "alt":
                    mods |= 1
        kl = target.lower()
        if kl in _KEYMAP:
            key_name, code, vk, text = _KEYMAP[kl]
        elif len(target) == 1:
            key_name = target
            code = f"Key{target.upper()}" if target.isalpha() else target
            vk = ord(target.upper()) if target.isalpha() else ord(target)
            text = ""
        else:
            key_name, code, vk, text = target, target, 0, ""
        base = {"key": key_name, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
                "modifiers": mods}
        self._p._conn.send("Input.dispatchKeyEvent",
                           {**base, "type": "rawKeyDown"},
                           session_id=self._p._sid, wait=False)
        if text:
            self._p._conn.send("Input.dispatchKeyEvent",
                               {**base, "type": "char", "text": text},
                               session_id=self._p._sid, wait=False)
        self._p._conn.send("Input.dispatchKeyEvent",
                           {**base, "type": "keyUp"},
                           session_id=self._p._sid, wait=False)

    def type(self, text: str, delay: float = 15):
        """Type like a human: per-character keyDown(with text)+keyUp, which
        fires keydown/keypress/input/keyup — what Angular's composer needs.
        Input.insertText is faster but silently no-ops on some focused
        contenteditables, so key events are the reliable path."""
        if not text:
            return
        for ch in text:
            vk = ord(ch.upper()) if ch.isalpha() else ord(ch)
            code = f"Key{ch.upper()}" if ch.isalpha() else (
                f"Digit{ch}" if ch.isdigit() else ch)
            common = {"key": ch, "code": code,
                      "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
            self._p._conn.send("Input.dispatchKeyEvent",
                               {**common, "type": "keyDown", "text": ch,
                                "unmodifiedText": ch},
                               session_id=self._p._sid, timeout=20)
            self._p._conn.send("Input.dispatchKeyEvent",
                               {**common, "type": "keyUp"},
                               session_id=self._p._sid, wait=False)
            if delay:
                time.sleep(min(delay, 25) / 1000.0)


class Page:
    def __init__(self, conn: _Conn, sid: str):
        self._conn = conn
        self._sid = sid
        self.keyboard = Keyboard(self)
        self._seen_errors = set()
        self._last_js_error = ""

    # -- helpers ------------------------------------------------
    def _send(self, method, params=None, timeout=30, wait=True):
        return self._conn.send(method, params or {}, session_id=self._sid,
                               timeout=timeout, wait=wait)

    def _send_async(self, method, params=None):
        return self._conn.send(method, params or {}, session_id=self._sid,
                              wait=False)

    # -- navigation --------------------------------------------
    def goto(self, url: str, wait_until: str = "commit", timeout: float = 60000):
        r = self._send("Page.navigate", {"url": url},
                       timeout=timeout / 1000.0)
        err = (r or {}).get("errorText")
        if err:
            raise CdpError(f"goto {url}: {err}")
        if wait_until not in ("commit", "none"):
            deadline = time.time() + timeout / 1000.0
            while time.time() < deadline:
                try:
                    st = self.evaluate("document.readyState")
                except Exception:
                    st = None
                if wait_until == "domcontentloaded" and st in ("interactive",
                                                             "complete"):
                    break
                if wait_until == "load" and st == "complete":
                    break
                time.sleep(0.3)
        return r

    def reload(self, timeout: float = 45000, wait_until: str = "commit"):
        self._send("Page.reload", {}, timeout=timeout / 1000.0)
        return True

    @property
    def url(self) -> str:
        try:
            return self.evaluate("document.location.href") or ""
        except Exception:
            return ""

    # -- js -----------------------------------------------------
    def evaluate(self, expression: str, arg: Any = None, timeout: float = 60):
        expr = expression
        if arg is not None:
            # Playwright-style "call this function with an argument".
            # Assign to a const first: `typeof async ({a}) => {}` is a
            # SyntaxError (typeof binds to `async`), which silently broke
            # every engine RPC. A const assignment parses for arrows, async
            # arrows and plain expressions alike.
            expr = ("((__a) => { const __f = (" + expression + ");"
                    " return (typeof __f === 'function') ? __f(__a) : __f; })"
                    "(" + json.dumps(arg) + ")")
        r = self._send("Runtime.evaluate",
                       {"expression": expr, "returnByValue": True,
                        "awaitPromise": True, "userGesture": True},
                       timeout=timeout)
        if not r:
            return None
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            # Surface the JS error instead of silently returning None —
            # a bare None made "create_project failed" undiagnosable.
            try:
                ex = r["exceptionDetails"]
                desc = (ex.get("exception") or {}).get("description") or \
                       ex.get("text") or "?"
                self._last_js_error = str(desc)[:300]
                if self._last_js_error not in self._seen_errors:
                    self._seen_errors.add(self._last_js_error)
                    print(f"[cdp] JS error: {self._last_js_error} | expr: "
                          f"{str(expression)[:120]!r}", flush=True)
            except Exception:
                pass
            return None
        if "value" in res:
            return res["value"]
        if res.get("type") == "undefined":
            return None
        return res.get("description")

    def wait_for_function(self, js: str, timeout: float = 30000,
                          **_kw) -> bool:
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            try:
                if self.evaluate(js):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        raise CdpError("wait_for_function timed out")

    def content(self) -> str:
        try:
            return self.evaluate("document.documentElement.outerHTML") or ""
        except Exception:
            return ""

    def screenshot(self, path: str = "", **_kw):
        try:
            r = self._send("Page.captureScreenshot", {"format": "png"})
            data = base64.b64decode((r or {}).get("data", ""))
            if path:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
            return data
        except Exception:
            return b""

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector=selector)

    def get_by_role(self, role: str, name: str = "") -> Locator:
        return Locator(self, role=role, name=name)

    def query_selector_count(self, selector: str) -> int:
        return Locator(self, selector=selector).count()

    # -- input -------------------------------------------------
    def mouse_click(self, x: float, y: float):
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        self._send_async("Input.dispatchMouseEvent",
                         {**base, "type": "mouseMoved", "buttons": 0})
        self._send_async("Input.dispatchMouseEvent",
                         {**base, "type": "mousePressed", "buttons": 1})
        self._send_async("Input.dispatchMouseEvent",
                         {**base, "type": "mouseReleased", "buttons": 0})

    # -- events ------------------------------------------------
    def on(self, event: str, handler: Callable):
        if event == "response":
            def _h(p, sid):
                try:
                    r = p.get("response", {})
                    rid = p.get("requestId", "")
                    resp = Response(self, r.get("url", ""), rid,
                                    r.get("mimeType", ""))
                    resp.status = int(r.get("status", 0) or 0)
                    handler(resp)
                except Exception:
                    pass
            handler._cdp_wrap = _h
            self._conn.on("Network.responseReceived", _h)
        elif event == "request":
            def _h(p, sid):
                try:
                    rq = p.get("request", {})
                    req = Request(rq.get("url", ""), rq.get("method", "GET"),
                                  rq.get("headers") or {}, "other",
                                  rq.get("postData") or "")
                    handler(req)
                except Exception:
                    pass
            handler._cdp_wrap = _h
            self._conn.on("Network.requestWillBeSent", _h)

    def remove_listener(self, event: str, handler: Callable):
        w = getattr(handler, "_cdp_wrap", None)
        if w:
            self._conn.off("Network.responseReceived"
                           if event == "response" else
                           "Network.requestWillBeSent", w)


# ────────────────────────────────────────────────────────────────
# context / browser / playwright-lookalike
# ────────────────────────────────────────────────────────────────
_CHROME_BASE = [
    "--headless", "--hide-scrollbars", "--mute-audio",
    "--no-first-run", "--no-default-browser-check",
    "--disable-field-trial-config", "--disable-component-update",
    "--disable-default-apps", "--disable-client-side-phishing-detection",
    "--disable-background-networking", "--disable-sync",
    "--disable-translate", "--metrics-recording-only",
    "--password-store=basic", "--use-mock-keychain",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--window-size=1280,900",
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Context:
    def __init__(self, browser: "Browser"):
        self._b = browser
        self._conn = browser._conn
        self._sid = browser._sid
        self._init_scripts: List[str] = []
        self._routes: List[Callable] = []
        self._fetch_on = False
        self._paused_ids: Dict[str, float] = {}
        self._closed = False
        self.page = Page(self._conn, self._sid)

    # -- pages -------------------------------------------------
    @property
    def pages(self) -> List[Page]:
        return [self.page]

    def new_page(self) -> Page:
        return self.page

    # -- setup -------------------------------------------------
    def add_init_script(self, script: str):
        self._init_scripts.append(script)
        try:
            self.page._send("Page.addScriptToEvaluateOnNewDocument",
                            {"source": script}, timeout=20)
        except Exception:
            pass

    def route(self, pattern: str, handler: Callable):
        """All handlers are consulted for every request (chained), unlike
        Playwright where the newest silently wins."""
        self._routes.append(handler)
        if not self._fetch_on:
            self._enable_fetch()

    def _enable_fetch(self):
        try:
            self.page._send("Fetch.enable",
                            {"patterns": [{"urlPattern": "*",
                                           "requestStage": "Request"}]},
                            timeout=20)
        except Exception:
            return
        self._fetch_on = True

        def _paused(p, sid):
            rid = p.get("requestId", "")
            rq = p.get("request", {}) or {}
            req = Request(rq.get("url", ""), rq.get("method", "GET"),
                          rq.get("headers") or {},
                          p.get("resourceType") or "other",
                          rq.get("postData") or "")
            chain = _Chain()
            route = Route(chain, req)
            for h in list(self._routes):
                try:
                    h(route)
                except Exception:
                    pass
                if chain.abort:
                    break
            # a request left paused forever hangs the page — track it so the
            # sweeper can release it if our continue silently failed
            self._paused_ids[rid] = time.time()

            def _release(params, fail=False):
                """Verify the release. Fire-and-forget hid failures: a request
                that stayed paused left the page's fetch promise unresolved,
                so the awaited Runtime.evaluate hung for its full 60s timeout
                and looked like a wedged renderer."""
                method = "Fetch.failRequest" if fail else "Fetch.continueRequest"
                for attempt in (0, 1):
                    try:
                        if fail:
                            self.page._send(method, params, timeout=15)
                        else:
                            # retry once WITHOUT header overrides: a rejected
                            # header is the common cause of a failed continue
                            p2 = {"requestId": params["requestId"]}
                            if attempt == 0 and "headers" in params:
                                p2["headers"] = params["headers"]
                            self.page._send(method, p2, timeout=15)
                        self._paused_ids.pop(rid, None)
                        return True
                    except Exception as e:
                        if attempt == 1:
                            print(f"[cdp] {method} failed for {rid[:12]}: "
                                  f"{str(e)[:100]} — sweeper will release",
                                  flush=True)
                return False

            try:
                if chain.abort:
                    _release({"requestId": rid, "errorReason": "Aborted"},
                             fail=True)
                elif chain.headers:
                    _release({"requestId": rid,
                              "headers": _clean_headers(chain.headers)})
                else:
                    _release({"requestId": rid})
            except Exception:
                pass

        self._conn.on("Fetch.requestPaused", _paused)
        # sweeper: release anything paused > 25s (silent-failure safety net)
        def _sweep():
            while not self._closed:
                time.sleep(5)
                now = time.time()
                for rid, ts in list(self._paused_ids.items()):
                    if now - ts > 25:
                        self._paused_ids.pop(rid, None)
                        try:
                            self.page._send_async("Fetch.continueRequest",
                                                  {"requestId": rid})
                        except Exception:
                            pass
        threading.Thread(target=_sweep, daemon=True).start()

    # -- cookies -----------------------------------------------
    def add_cookies(self, cookies: List[dict]):
        items = []
        for c in cookies or []:
            it = {"name": c.get("name", ""), "value": c.get("value", ""),
                  "domain": c.get("domain") or ".google.com",
                  "path": c.get("path") or "/",
                  "secure": bool(c.get("secure", True)),
                  "httpOnly": bool(c.get("httpOnly", False))}
            ss = str(c.get("sameSite") or "").lower()
            it["sameSite"] = {"none": "None", "lax": "Lax",
                              "strict": "Strict"}.get(ss, "Lax")
            exp = c.get("expires")
            try:
                exp = float(exp) if exp not in (None, -1, "") else None
            except Exception:
                exp = None
            if exp and exp > 0:
                it["expires"] = exp
            # CDP rejects SameSite=None without Secure
            if it["sameSite"] == "None":
                it["secure"] = True
            if it["name"]:
                items.append(it)
        if not items:
            return
        try:
            self.page._send("Network.setCookies", {"cookies": items},
                            timeout=30)
        except Exception:
            # fall back to one-at-a-time so a single bad cookie can't drop
            # the whole jar
            for it in items:
                try:
                    self.page._send("Network.setCookies", {"cookies": [it]},
                                    timeout=15)
                except Exception:
                    pass

    def cookies(self) -> List[dict]:
        try:
            r = self.page._send("Network.getAllCookies", {}, timeout=30) or {}
            out = []
            for c in r.get("cookies", []):
                out.append({"name": c.get("name"), "value": c.get("value"),
                            "domain": c.get("domain"), "path": c.get("path"),
                            "expires": c.get("expires"),
                            "httpOnly": c.get("httpOnly"),
                            "secure": c.get("secure"),
                            "sameSite": c.get("sameSite")})
            return out
        except Exception:
            return []

    def close(self):
        try:
            self._conn.send("Target.closeTarget",
                            {"targetId": self._b._target_id}, timeout=10)
        except Exception:
            pass


class Browser:
    """Launches chrome-headless-shell and attaches to its first page target."""

    def __init__(self, exe: str, profile: str, args: List[str],
                 env: Optional[dict] = None):
        self.exe = exe
        self.profile = profile
        self._proc: Optional[subprocess.Popen] = None
        self._conn: Optional[_Conn] = None
        self._extra_args: List[str] = list(args or [])
        self._target_id = ""
        self._sid = ""

    def start(self, timeout: float = 60.0):
        os.makedirs(self.profile, exist_ok=True)
        port = _free_port()
        self._port = port
        cmd = [self.exe, *_CHROME_BASE, *self._args_full(port)]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=os.environ.copy(), start_new_session=True)
        ws_url = self._wait_for_devtools(port, timeout)
        self._conn = _Conn(ws_url)
        # attach to the page target
        targets = (self._conn.send("Target.getTargets") or {}).get(
            "targetInfos", [])
        page = None
        for t in targets:
            if t.get("type") == "page":
                page = t
                break
        if page is None:
            page = self._conn.send("Target.createTarget",
                                   {"url": "about:blank"})
            self._target_id = page["targetId"]
            targets = (self._conn.send("Target.getTargets") or {}).get(
                "targetInfos", [])
            for t in targets:
                if t.get("targetId") == self._target_id:
                    page = t
                    break
        self._target_id = page["targetId"]
        att = self._conn.send("Target.attachToTarget",
                              {"targetId": self._target_id, "flatten": True})
        self._sid = att["sessionId"]
        # domains the engine needs
        for m in ("Page.enable", "Runtime.enable", "Network.enable",
                  "DOM.enable"):
            try:
                self._conn.send(m, {}, session_id=self._sid, timeout=20)
            except Exception:
                pass
        # INPUT-CRITICAL: a headless page reports "not focused", so Chrome
        # silently DROPS dispatched key events (typing appeared to no-op).
        # Focus emulation makes the renderer believe it has focus.
        try:
            self._conn.send("Emulation.setFocusEmulationEnabled",
                            {"enabled": True}, session_id=self._sid, timeout=20)
        except Exception:
            pass
        try:
            self._conn.send("Page.bringToFront", {}, session_id=self._sid,
                            timeout=20)
        except Exception:
            pass
        return self

    def _args_full(self, port: int) -> List[str]:
        return [*self._extra_args, f"--user-data-dir={self.profile}",
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1", "about:blank"]

    def set_args(self, extra: List[str]):
        self._extra_args = list(extra)

    def _wait_for_devtools(self, port: int, timeout: float) -> str:
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{port}/json/version"
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise CdpError("chrome exited during launch "
                               f"(code {self._proc.returncode})")
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                ws = data.get("webSocketDebuggerUrl")
                if ws:
                    return ws
            except Exception:
                time.sleep(0.4)
        raise CdpError(f"devtools endpoint never came up on :{port}")

    def stop(self):
        try:
            if self._conn:
                self._conn.send("Browser.close", wait=False)
        except Exception:
            pass
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=10)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._conn = None


class _Chromium:
    def __init__(self, pw: "Playwright"):
        self._pw = pw

    def launch_persistent_context(self, user_data_dir: str,
                                  executable_path: str = "",
                                  headless: bool = True,
                                  args: Optional[List[str]] = None,
                                  user_agent: str = "",
                                  viewport: Optional[dict] = None,
                                  proxy: Optional[dict] = None,
                                  **kwargs) -> Context:
        extra = list(args or [])
        if user_agent:
            extra.append(f"--user-agent={user_agent}")
        if viewport:
            extra.append(f"--window-size={int(viewport.get('width', 1280))},"
                         f"{int(viewport.get('height', 900))}")
        if proxy:
            srv = (proxy.get("server") or "").strip()
            if srv:
                extra.append(f"--proxy-server={srv}")
            if proxy.get("username"):
                extra.append(f"--proxy-auth={proxy['username']}:"
                             f"{proxy.get('password', '')}")
        b = Browser(executable_path or "", user_data_dir, extra)
        b.start()
        self._pw._browser = b
        return Context(b)


class Playwright:
    def __init__(self):
        self._browser: Optional[Browser] = None
        self.chromium = _Chromium(self)

    def stop(self):
        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception:
                pass
            self._browser = None


def connect_over_ws(*_a, **_k):
    raise NotImplementedError("use sync_playwright()")


class _SyncCtx:
    def __init__(self, pw: Playwright):
        self._pw = pw

    def start(self) -> Playwright:
        return self._pw

    def __enter__(self):
        return self._pw

    def __exit__(self, *a):
        self._pw.stop()


def sync_playwright() -> _SyncCtx:
    """Name-compatible entry point so the engine's code doesn't change."""
    return _SyncCtx(Playwright())
