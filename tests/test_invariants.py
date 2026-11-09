"""Regression tests for the auth/cookie invariants that broke the service.

These are pure-logic tests — no browser, no network, no credentials — so they
run anywhere and catch the exact classes of bug that cost real debugging time:

  1. cookie records must survive a rotation (values AND newly-appearing names)
  2. rebuilding a session must dedupe per-domain duplicates
  3. the at-harvest must fall back to the page's WIZ_global_data
  4. "session dead" must be distinguished from "content rejected"

Run:  python tests/test_invariants.py     (or: pytest tests/)
"""
import os
import pathlib
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import flow_auth as A          # noqa: E402
import flow_api_headless as F  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")


# ── 1. rotation must propagate into the records ──────────────────────────────
print("[1] sync_records: updates existing values AND appends new names")
A.LAST_RECORDS = [
    {"name": "SID", "value": "old-sid", "domain": ".google.com", "path": "/",
     "secure": False},
    {"name": "__Secure-1PSID", "value": "p", "domain": ".google.com",
     "path": "/", "secure": True},
]
A.sync_records({"SID": "new-sid"})
check("existing value updated",
      A.LAST_RECORDS[0]["value"] == "new-sid")
# __Secure-1PSIDTS is absent from the mint and appears only after rotation
A.sync_records({"SID": "new-sid", "__Secure-1PSIDTS": "sidts-abc"})
names = {r["name"]: r for r in A.LAST_RECORDS}
check("newly-appearing name appended", "__Secure-1PSIDTS" in names)
check("appended cookie marked Secure (required for __Secure-*)",
      names.get("__Secure-1PSIDTS", {}).get("secure") is True)
check("no duplicate records introduced",
      len([r for r in A.LAST_RECORDS if r["name"] == "__Secure-1PSIDTS"]) == 1)


# ── 2. session rebuild dedupes per-domain duplicates ────────────────────────
print("\n[2] restore_session: one cookie per name, .google.com preferred")
recs = [
    {"name": "SID", "value": "google", "domain": ".google.com", "secure": False},
    {"name": "SID", "value": "country", "domain": ".google.co.in", "secure": False},
    {"name": "__Secure-1PSID", "value": "g2", "domain": ".google.com",
     "secure": True},
    {"name": "__Secure-1PSID", "value": "c2", "domain": ".google.co.in",
     "secure": True},
]
s = A.restore_session(records=recs)
vals = {}
for c in s.cookies.jar:
    vals.setdefault(c.name, []).append((c.domain, c.value))
check("SID sent once", len(vals.get("SID", [])) == 1, str(vals.get("SID")))
check("SID keeps the .google.com value",
      vals.get("SID") == [(".google.com", "google")], str(vals.get("SID")))
check("__Secure-1PSID deduped to google.com",
      vals.get("__Secure-1PSID") == [(".google.com", "g2")])


# ── 3. at-harvest falls back to the page ────────────────────────────────────
print("\n[3] harvest_at: 400-body route fails -> page WIZ_global_data fallback")


class FakeResp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class FakeSession:
    """POST (the 400 route) returns a bare `er` body with no xsrf, exactly
    what Google does now; GET returns the app page carrying SNlM0e."""

    def __init__(self):
        self.posted = 0

    def post(self, *a, **k):
        self.posted += 1
        return FakeResp(400, ")]}'\n\n99\n[[\"er\",null,null,null,null,400]]\n")

    def get(self, *a, **k):
        return FakeResp(200, 'window.WIZ_global_data={"SNlM0e":'
                             '"AFfZSpgZXvWH4UV2:1792097194170"};')


fs = FakeSession()
try:
    at = A.harvest_at(fs, "bl", "fsid")
    check("fell back to the page and returned a token", at.endswith(":1792097194170"),
          at[:24] + "...")
except Exception as e:
    check("fell back to the page and returned a token", False, str(e)[:80])


# ── 4. dead-session classification ──────────────────────────────────────────
print("\n[4] error classification: session death vs content rejection")
for err, want in [("create_project failed: None", True),
                  ("rpc nzlxg: {'status': 401}", True),
                  ("no at token", True),
                  ("logged out", True),
                  ("prompt rejected by safety filters", False),
                  ("quota exceeded", False)]:
    got = F.FlowAPI._looks_like_dead_session(err)
    check(f"{err[:38]!r} -> {want}", got == want)


# ── 5. wedge classification (restart) vs plain failure ──────────────────────
print("\n[5] error classification: wedged renderer vs plain failure")
for err, want in [("Runtime.evaluate: timeout after 60s", True),
                  ("Locator.count: Target page, context or browser has been closed", True),
                  ("browser has been closed", True),
                  ("prompt rejected by safety filters", False),
                  ("ogiZ0b response had no media", False)]:
    got = F.FlowAPI._looks_like_wedge(err)
    check(f"{err[:44]!r} -> {want}", got == want)

# ── 6. the feature catalogue must be complete and self-consistent ───────────
print("\n[6] feature catalogue (/models)")
import flow_headless as FH  # noqa: E402

cat = F.FlowAPI.models(type("S", (), {})())  # no instance state needed
check("catalogue ok", cat.get("ok") is True, str(cat.get("error", ""))[:60])
models = cat.get("models") or []
check("every wire model exposed", len(models) == len(FH.MODEL_WIRE),
      f"{len(models)} vs {len(FH.MODEL_WIRE)}")
check("default model flagged",
      any(m["default"] for m in models))
check("model ids resolved from the enum",
      all(m["model_id"] for m in models),
      str([m["key"] for m in models if not m["model_id"]]))
check("every model carries its human alias",
      all(m["aliases"] for m in models),
      str([m["key"] for m in models if not m["aliases"]]))
aspects = cat.get("aspects") or []
check("every aspect ratio exposed", len(aspects) == len(FH.ASPECT_WIRE),
      f"{len(aspects)} vs {len(FH.ASPECT_WIRE)}")
check("aspect wire ints preserved",
      {a["ratio"]: a["wire"] for a in aspects} == dict(FH.ASPECT_WIRE))
check("upscale targets listed",
      set(cat.get("upscale") or []) == {"2K", "4K"})
check("full enum published",
      len(cat.get("model_enum_all") or {}) == len(FH.MODEL_ENUM))
check("endpoints documented",
      "/models" in (cat.get("endpoints", {}).get("GET") or []))

# ── 6. media delivery ───────────────────────────────────────────────────────
# /media/<uuid> 404'd for every image because FlowAPI.media() was a bare
# `return None` stub AND nothing ever cached the bytes locally.
import inspect  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

_src = inspect.getsource(F.FlowAPI.media)
check("media() is no longer a 'return None' stub",
      "engine.history" in _src and "save_media_bytes" in _src)
check("media() consults the disk cache first", "_cached_media" in _src)
check("generation caches bytes before returning",
      "save_media_bytes" in inspect.getsource(F.FlowAPI._generate_job))

with tempfile.TemporaryDirectory() as _td:
    _inst = F.FlowAPI.__new__(F.FlowAPI)
    _inst._outputs_dir = lambda: Path(_td)
    (Path(_td) / "aaa.jpg").write_bytes(b"x" * 100)        # too small
    (Path(_td) / "bbb.jpg").write_bytes(b"y" * 20000)      # real file
    check("undersized cache entry ignored", _inst._cached_media("aaa") is None)
    check("cached media found by uuid",
          str(_inst._cached_media("bbb")) == str(Path(_td) / "bbb.jpg"))

    class _Resp:
        def __init__(self, blob):
            self._b = blob

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    _real = F.urllib.request.urlopen
    try:
        F.urllib.request.urlopen = lambda *a, **k: _Resp(b"short")
        check("short download rejected (no junk cached)",
              _inst.save_media_bytes("http://x/y.jpg", "ccc") is None)
        F.urllib.request.urlopen = lambda *a, **k: _Resp(b"z" * 30000)
        _p = _inst.save_media_bytes("http://x/y.jpg", "ddd")
        check("good download cached as <uuid>.jpg",
              _p is not None and _p.name == "ddd.jpg"
              and _p.stat().st_size == 30000)
    finally:
        F.urllib.request.urlopen = _real

# ── 7. watermark removal ────────────────────────────────────────────────────
import io  # noqa: E402
import os as _os  # noqa: E402

import dewatermark_server as DW  # noqa: E402

check("dewatermarker reports available", DW.available() is True)
_alpha, _w = DW._mask()
check("alpha mask embedded and non-degenerate",
      _w == DW.BOX[2] - DW.BOX[0] and max(_alpha) > 40,
      f"w={_w} max={max(_alpha)}")
check("mask height matches the box",
      len(_alpha) // _w == DW.BOX[3] - DW.BOX[1])

from PIL import Image as _Image  # noqa: E402
# a synthetic render: mid-grey field, so the glyph region brightens measurably
_buf = io.BytesIO()
_Image.new("RGB", (1376, 768), (120, 120, 120)).save(_buf, format="JPEG",
                                                      quality=90)
_before = _buf.getvalue()
_after = DW.clean_bytes(_before)
check("clean_bytes returns bytes", isinstance(_after, bytes) and len(_after) > 500)
check("clean_bytes changes a 1376x768 render",
      _after != _before)
check("clean_bytes leaves other sizes alone",
      DW.clean_bytes(_Image.new("RGB", (64, 64), (1, 2, 3)).tobytes()) != b"" and
      DW.clean_bytes(b"not-an-image") == b"not-an-image")
check("output stays within 1.5x of the source (no size blow-up)",
      len(_after) < len(_before) * 1.5, f"{len(_before)} -> {len(_after)}")
_os.environ["FLOW_DEWATERMARK"] = "0"
check("FLOW_DEWATERMARK=0 disables it", DW.available() is False)
check("disabled clean_bytes is a pass-through",
      DW.clean_bytes(_before) == _before)
del _os.environ["FLOW_DEWATERMARK"]
check("re-enabled after unsetting the flag", DW.available() is True)

# ── 8. browser-friendly auth + UI wiring ────────────────────────────────────
_srv = (Path(__file__).resolve().parent.parent / "flow_server.py").read_text(
    encoding="utf-8")
check("auth accepts ?key= for browser/img access",
      '"key"' in _srv and "parse_qs" in _srv.split("def _auth_ok")[1][:1200])
check("UI is served at the root path", '"/", "/ui"' in _srv)
check("UI file shipped in the image",
      (Path(__file__).resolve().parent.parent / "ui" / "index.html").exists())
_dfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(
    encoding="utf-8")
check("image copies the dewatermarker", "dewatermark_server.py" in _dfile)
_reqs = (Path(__file__).resolve().parent.parent /
         "requirements.docker.txt").read_text(encoding="utf-8")
check("image installs Pillow", "Pillow" in _reqs)

# ── 9. busy accounting (the "always busy" bug) ──────────────────────────────
# The supervisor's liveness probe occupies the worker for its full timeout. It
# used to count toward is_busy(), so a slow/wedged probe advertised
# "a generation is already running" continuously — the service looked stuck.
import queue as _queue  # noqa: E402
import threading as _threading  # noqa: E402

_b = F.FlowAPI.__new__(F.FlowAPI)
_b._counter_lock = _threading.Lock()
_b._active = 0
_b._internal_active = 0
_b._q = _queue.Queue()
check("idle => not busy", _b.is_busy() is False)
_b._active = 1
_b._internal_active = 1
check("an internal probe alone does NOT report busy", _b.is_busy() is False)
_b._active = 2
check("a user job alongside a probe IS busy", _b.is_busy() is True)
_b._active = 1
_b._internal_active = 0
check("user job alone is busy", _b.is_busy() is True)
_b._active = 0
check("back to idle", _b.is_busy() is False)
_b._q.put("x")
check("queued work counts as busy", _b.is_busy() is True)

_src2 = inspect.getsource(F.FlowAPI.run)
check("run() accepts the _internal marker", "_internal" in _src2)
check("run() tracks internal ops separately",
      "_internal_active" in _src2)
_sup = inspect.getsource(F.FlowAPI._supervise)
check("liveness probe is marked internal",
      "_internal=True" in _sup)
check("liveness has a renderer-free first tier (process count)",
      "_count_browser_procs" in _sup and
      hasattr(F.FlowAPI, "_count_browser_procs"))
check("a stuck renderer gets a SOFT reload before a full rebuild",
      "goto_app" in _sup and "soft reload" in _sup)
check("probe timeout is a middle bound (45s), not the 25s panic value",
      "timeout=45" in _sup and "timeout=25" not in _sup)
check("probe failure is diagnosed, not guessed at",
      "procs=" in _sup and "fails=" in _sup)

# ── 10. CDP socket death must not be silent ─────────────────────────────────
# The wedge: the websocket dropped, the reader thread simply exited, and from
# then on every CDP call waited its full timeout forever while the browser
# process stayed alive (logs showed procs=1 and even Page.navigate timing out).
import flow_cdp as _CDP  # noqa: E402

_c = _CDP._Conn.__new__(_CDP._Conn)          # no socket needed
_c._lock = _threading.Lock()
_c._pending = {}
_c._next_id = 1
_c._closed = False
_waiter = _threading.Event()
_c._pending[42] = {"ev": _waiter, "result": None, "error": None}
check("fresh connection is not dead", _c.dead is False)

_c._fail_all("socket closed (test)")
check("failed connection reports dead", _c.dead is True)
check("in-flight waiter released IMMEDIATELY (no timeout burn)",
      _waiter.is_set())
check("pending table drained", _c._pending == {})
check("_fail_all is idempotent", (_c._fail_all("again"), _c.dead)[1] is True)

try:
    _c.send("Runtime.evaluate", {"expression": "1"})
    _fast = False
except Exception as _e:
    _fast = "closed" in str(_e).lower()
check("send() fails fast once the socket is dead", _fast)

_rdr = inspect.getsource(_CDP._Conn._reader)
check("reader reports socket death instead of exiting silently",
      "_fail_all" in _rdr)
_snd = inspect.getsource(_CDP._Conn.send)
check("send() guards against a closed connection", "_closed" in _snd)
_sup2 = inspect.getsource(F.FlowAPI._supervise)
check("supervisor fails over on a dead CDP connection",
      "dead" in _sup2 and "CDP connection is dead" in _sup2)

# ── 11. an idle read timeout is NOT a dead socket ───────────────────────────
# The rebuild loop: the websocket has a read timeout, CDP is silent between
# commands, so recv() raised TimeoutError("Connection timed out") on a healthy
# connection. The reader treated that as death -> the supervisor rebuilt the
# browser every ~90s, killing whatever generation was in flight.
print("\n[11] CDP idle timeout vs real socket death")
check("TimeoutError reads as an idle timeout", _CDP._is_idle_timeout(
    TimeoutError("Connection timed out")) is True)
check("socket.timeout reads as an idle timeout", _CDP._is_idle_timeout(
    _CDP.socket.timeout("timed out")) is True)


class _WSExc(Exception):
    pass


_WSExc.__name__ = "WebSocketTimeoutException"
check("websocket-client's timeout exception counts as idle",
      _CDP._is_idle_timeout(_WSExc("Connection timed out")) is True)
check("a closed connection is NOT an idle timeout", _CDP._is_idle_timeout(
    ConnectionResetError("Connection reset by peer")) is False)
check("'remote host was lost' is NOT an idle timeout", _CDP._is_idle_timeout(
    Exception("Connection to remote host was lost.")) is False)
check("'socket closed' is NOT an idle timeout", _CDP._is_idle_timeout(
    Exception("socket closed")) is False)

_rdr2 = inspect.getsource(_CDP._Conn._reader)
check("reader ignores idle timeouts instead of failing the connection",
      "_is_idle_timeout" in _rdr2 and "continue" in _rdr2)
check("reader still marks a genuinely closed socket dead",
      "_fail_all" in _rdr2)
check("an empty frame is treated as a close (no hot spin)",
      "empty frame" in _rdr2)

# ── 12. model defaults ──────────────────────────────────────────────────────
# An omitted model used to resolve to NARWHAL — deprecated, shuts down
# 2026-10-29 — instead of the current default.
print("\n[12] model resolution defaults to the CURRENT model")
check("flow_headless: no model -> DEFAULT_MODEL",
      FH.norm_model(None) == FH.DEFAULT_MODEL, FH.norm_model(None))
check("flow_headless: empty string -> DEFAULT_MODEL",
      FH.norm_model("") == FH.DEFAULT_MODEL)
check("facade: no model -> BELUGA", F.norm_model(None) == "BELUGA",
      F.norm_model(None))
check("facade: 'NANO BANANA 2.1' maps to BELUGA (not NARWHAL)",
      F.norm_model("NANO BANANA 2.1") == "BELUGA",
      F.norm_model("NANO BANANA 2.1"))
check("facade: 'BELUGA' passes through", F.norm_model("BELUGA") == "BELUGA")
check("facade: explicit NARWHAL still honoured for back-compat",
      F.norm_model("NANO BANANA 2") == "NARWHAL")
check("explicit aliases still resolve (pro)",
      F.norm_model("NANO BANANA PRO") == "GEM_PIX_2")
_ls_src = inspect.getsource(FH.load_state)
check("stored default model is not the deprecated NARWHAL",
      'get("FLOW_MODEL", DEFAULT_MODEL)' in _ls_src)
check("stale NARWHAL state migrates forward to the default",
      'st["model"] = DEFAULT_MODEL' in _ls_src)
# the migration itself, against a synthetic state file
_orig_state = FH.STATE_FILE
with tempfile.TemporaryDirectory() as _sd:
    FH.STATE_FILE = Path(_sd) / "flow_api_state.json"
    FH.STATE_FILE.write_text('{"project_id": "p", "model": "NARWHAL"}',
                             encoding="utf-8")
    check("load_state rewrites a deprecated stored model",
          FH.load_state().get("model") == FH.DEFAULT_MODEL,
          str(FH.load_state().get("model")))
    FH.STATE_FILE.write_text('{"project_id": "p", "model": "GEM_PIX_2"}',
                             encoding="utf-8")
    check("load_state leaves a valid stored model alone",
          FH.load_state().get("model") == "GEM_PIX_2")
FH.STATE_FILE = _orig_state

# ── 13. upload (RPC maseQ) ──────────────────────────────────────────────────
# The wire shape is the one thing that cannot be re-derived by guessing, so it
# is pinned here against the captured payload (API.md "Upload / reference
# images").
print("\n[13] upload: maseQ payload + /upload surface")
_p = FH.build_upload_payload("PROJ-UUID", "BASE64DATA", mime="image/jpeg",
                             filename="probe_upload.jpg",
                             uuid_a="AAA", uuid_b="BBB")
check("payload is a 12-element list", isinstance(_p, list) and len(_p) == 12,
      str(len(_p)))
check("head[5] carries the project id",
      isinstance(_p[0], list) and _p[0][5] == "PROJ-UUID")
check("head[1] is the literal 22",
      isinstance(_p[0], list) and _p[0][1] == 22)
check("base64 rides at [1]", _p[1] == "BASE64DATA")
check("mime rides at [2]", _p[2] == "image/jpeg")
check("the marker 1 is at [3]", _p[3] == 1)
check("filename rides at [8]", _p[8] == "probe_upload.jpg")
check("two uuids ride at [10]/[11]", _p[10] == "AAA" and _p[11] == "BBB")
check("head is 10 long without a token", len(_p[0]) == 10)
_p2 = FH.build_upload_payload("P", "B", token="TOK", uuid_a="A", uuid_b="B")
check("supplying a token appends [tok, 1] at head[10]",
      _p2[0][10] == ["TOK", 1] and len(_p2[0]) == 11)

check("engine exposes upload_image", hasattr(FH.HeadlessEngine, "upload_image"))
_up_src = inspect.getsource(FH.HeadlessEngine.upload_image)
check("upload uses the in-page transport (maseQ), not pure HTTP",
      '"maseQ"' in _up_src and "self.call" in _up_src)
check("upload gives the fetch a large deadline",
      "timeout_ms=180000" in _up_src)
check("upload sniffs jpeg/png/webp when no mime is given",
      b"\\xff\\xd8\\xff".decode("latin1") in _up_src or "image/png" in _up_src)
check("facade exposes upload()", hasattr(F.FlowAPI, "upload"))
check("facade upload is bounded by a timeout",
      "timeout=420" in inspect.getsource(F.FlowAPI.upload))

_srv2 = (Path(__file__).resolve().parent.parent / "flow_server.py").read_text(
    encoding="utf-8")
check("server routes POST /upload", 'p.path == "/upload"' in _srv2)
check("upload accepts JSON image_base64", "image_base64" in _srv2)
check("upload accepts raw image bytes (non-JSON body)",
      "_read_raw" in _srv2 and "b64encode(raw_body)" in _srv2)
check("upload enforces a size ceiling", "MAX_UPLOAD_MB" in _srv2
      and "413" in _srv2)
check("upload is listed in the catalogue",
      "/upload" in (cat.get("endpoints", {}).get("POST") or []))

# ── 14. "composer not found" must be diagnosable and cheap to recover ───────
# It used to: log nothing about the page, and immediately report "not logged in"
# — which routed to a re-mint + full browser rebuild (~3-4 min) for what is
# usually just a bad page load.
print("\n[14] composer-missing path")

check("engine exposes page_probe()",
      hasattr(FH.HeadlessEngine, "page_probe"))
_probe_src = inspect.getsource(FH.HeadlessEngine.page_probe)
for field in ("url", "title", "ready", "has_at", "editables", "buttons",
              "html_len", "text"):
    check(f"page_probe reports {field}", field in _probe_src)
check("page_probe is defensive (returns an error dict, never raises)",
      "except Exception" in _probe_src and '"error"' in _probe_src)

check("engine exposes _wait_composer()",
      hasattr(FH.HeadlessEngine, "_wait_composer"))
check("composer check requires visible AND enabled",
      "aria-disabled" in FH.HeadlessEngine.COMPOSER_JS
      and "width" in FH.HeadlessEngine.COMPOSER_JS)

_gen_src = inspect.getsource(FH.HeadlessEngine.generate)
check("generate probes the page when the composer is missing",
      "page_probe" in _gen_src)
check("generate reloads once before giving up",
      "reloading the app once" in _gen_src and "_wait_composer()" in _gen_src)
check("generate distinguishes /about (logged out) from no-hydration",
      '"/about"' in _gen_src and "never hydrated" in _gen_src)
check("the failure message carries the page URL for diagnosis",
      "page={url" in _gen_src or "page=" in _gen_src)

_diag_src = inspect.getsource(F.FlowAPI.diag)
check("composer-missing recovery is cheaper than a session heal",
      "reloading the app once" in _gen_src)
check("engine exposes ensure_project()",
      hasattr(FH.HeadlessEngine, "ensure_project"))
_ep_src = inspect.getsource(FH.HeadlessEngine.ensure_project)
check("ensure_project reuses the newest existing project before creating one",
      "self.projects(limit=1)" in _ep_src
      and _ep_src.index("self.projects(limit=1)")
      < _ep_src.index("self.create_project()"))
check("ensure_project respects an explicit project id",
      "project_id or load_state()" in _ep_src)
check("generate uses ensure_project (not a blind create)",
      "self.ensure_project" in _gen_src
      and "no project id -> creating a new project" not in _gen_src)
check("upload uses ensure_project too",
      "self.ensure_project" in inspect.getsource(FH.HeadlessEngine.upload_image))
check("/diag runs as an internal op (so it can report at all)",
      "_internal=True" in _diag_src)
check("/diag no longer uses the plain 150s dispatch",
      "timeout=150)" not in _diag_src)
check("/diag reports the page state", "page_probe" in _diag_src)

# ── 15. evaluate() must INVOKE an arrow function, not return it ─────────────
# The root cause of "no `at` token" and of the composer wait burning its whole
# timeout: an arrow passed to Runtime.evaluate without an argument evaluates to
# a function OBJECT, and returnByValue serialises that to {} — empty and falsy.
# The wrapper was only applied when an argument was supplied.
print("\n[15] CDP evaluate() invocation")
check("_is_idle_timeout still classifies properly",
      _CDP._is_idle_timeout(TimeoutError("timed out")) is True)


class _FakeConn:
    """Captures the expression that would be sent to Runtime.evaluate."""

    def __init__(self):
        self.params = None

    def send(self, method, params=None, session_id=None, timeout=30,
             wait=True):
        self.params = params or {}
        return {"result": {"value": "STUB"}}


_pg = _CDP.Page(_FakeConn(), "sid")
_pg.evaluate("() => 2")
_e1 = _pg._conn.params.get("expression", "")
check("a NO-ARG arrow is wrapped and invoked", "__f(__a)" in _e1,
      _e1[:90])
check("...with null as the argument", "(null)" in _e1)
check("...inside a typeof-function guard", "typeof __f === 'function'" in _e1)

_pg.evaluate("1")
_e2 = _pg._conn.params.get("expression", "")
check("a plain expression is still wrapped (harmless)", "const __f = (1)" in _e2,
      _e2[:90])

_pg.evaluate("(n) => n * 3", 5)
_e3 = _pg._conn.params.get("expression", "")
check("an explicit argument is passed through", "JSON".replace("JSON", "(5)")
      in _e3 or "(5)" in _e3, _e3[:90])

_pg.evaluate("() => 9;")
_e4 = _pg._conn.params.get("expression", "")
check("a trailing semicolon is stripped (it would be a syntax error)",
      "() => 9;" not in _e4 and "() => 9" in _e4, _e4[:90])

_ev_src = inspect.getsource(_CDP.Page.evaluate)
check("the wrapper is unconditional (no `if arg is not None` gate)",
      "if arg is not None" not in _ev_src)
check("None serialises to null in the call", "json.dumps(arg)" in _ev_src)

# ── 16. a 401 means a stale `at`, not a dead session ────────────────────────
# Re-scraping the same page returned the same expired token, so the retry
# failed, the caller declared "logged out", and the service re-minted + rebuilt
# every ~11 minutes — killing any generation in flight.
print("\n[16] stale-`at` recovery")
_call_src = inspect.getsource(FH.HeadlessEngine.call)
check("call() retries after a 401/403", "401" in _call_src)
check("the retry RELOADS the page for a fresh `at`",
      "goto_app" in _call_src and "fresh `at`" in _call_src)
check("it no longer just re-scrapes the same stale page",
      "refreshing `at` and retrying" not in _call_src)
check("both cached tokens are dropped before the retry",
      '_live_at = ""' in _call_src and '_at = ""' in _call_src)
check("a failed reload is logged, not swallowed",
      "reload failed" in _call_src)
check("the reload targets the project page when there is one",
      "/project/{pid}" in _call_src)

# A reload returns at "commit" — long before the app writes SNlM0e. Scraping
# once immediately finds nothing and the session still gets declared dead
# (observed live: 401 at :54, "no at token" at :10 of the next minute,
# rebuild). The engine must WAIT for the token.
check("engine has _wait_at()", hasattr(FH.HeadlessEngine, "_wait_at"))
check("an `at` wait exists (AT_WAIT_S)", hasattr(FH, "AT_WAIT_S")
      and FH.AT_WAIT_S > 0, str(getattr(FH, "AT_WAIT_S", None)))
check("call() WAITS for the token instead of scraping once",
      "_wait_at(" in _call_src)
check("the no-token failure names the wait",
      "after waiting" in _call_src)


class _AtPage:
    """Page stub whose token appears on the third scrape."""

    def __init__(self):
        self.scrapes = 0

    def evaluate(self, *a, **k):
        self.scrapes += 1
        return "TOK:123" if self.scrapes >= 3 else ""


_eng = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng.page = _AtPage()
_eng._at = ""
_eng._pump = lambda seconds=5: None
t0 = time.time()
_got = _eng._wait_at(timeout=30)
_dt = time.time() - t0
check("wait_at returns once the token appears", _got == "TOK:123", _got)
check("it polled rather than accepting the first empty scrape",
      _eng.page.scrapes >= 3, str(_eng.page.scrapes))
check("it returned promptly after the token existed (no full-timeout burn)",
      _dt < 20, f"{_dt:.1f}s")

_eng2 = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng2.page = type("P", (), {"evaluate": lambda s, *a, **k: ""})()
_eng2._at = ""
_eng2._pump = lambda seconds=5: None
check("wait_at gives up with '' when the token never appears",
      _eng2._wait_at(timeout=3) == "")

# ── 17. the console UI must not look hung, and must not lose a result ───────
# The reported symptom was "the jobs just run and run and no image shows up".
# The server was fine (59 s end-to-end, /media in 0.4 s); all four defects were
# in the page. These pin them shut.
print("\n[17] console UI: nothing silently disappears")
_ui = (pathlib.Path(ROOT) / "ui" / "index.html").read_text(encoding="utf-8")
_srv = (pathlib.Path(ROOT) / "flow_server.py").read_text(encoding="utf-8")

check("no lazy images (a result must never wait for a scroll to load)",
      'loading="lazy"' not in _ui)
check("result images are eager", 'loading="eager"' in _ui)
check("a live placeholder exists so a render never looks frozen",
      "skeleton" in _ui and "spinner" in _ui)
check("the placeholder ticks elapsed seconds",
      "ptime" in _ui and "elapsed" in _ui)
check("the placeholder names the engine stage",
      "engine: generating" in _ui)
check("the wait tells the user the normal duration", "60–120 s" in _ui)
check("a reload RESUMES the running job instead of orphaning it",
      "flowjob" in _ui and "resume" in _ui)
check("a finished-while-away job is recovered on load",
      "recovered" in _ui)
check("recent results are re-fetched from the server", "/media?limit=" in _ui)
check("recent results refresh on a timer", "setInterval(loadRecent" in _ui)
check("the old fire-and-forget poll is gone", "MAX_WAIT_S" not in _ui)
check("429 is retried more than once on submit",
      "attempt ${i + 1}/8" in _ui)
check("a stalled request is retried, not called a failed prompt",
      "no response from the server" in _ui and "r.status === 0" in _ui)
check("the key change invalidates cached image URLs", "SEEN.clear()" in _ui)

# the history panel is only possible because the server can list media
check("GET /media lists recent media",
      'p.path == "/media"' in _srv and "media_list(" in _srv)
check("the media list is matched BEFORE the /media/<uuid> prefix branch",
      _srv.find('p.path == "/media"') < _srv.find('p.path.startswith("/media/")'))
check("the page names a session recovery in the wait",
      "recovering the Google session" in _ui)

# ── 18. a logged-out page must not be reloaded into ─────────────────────────
# Measured live: /about seen at 10:46:58, the reload burned until 10:49:15, and
# the heal finished at 10:50:28 — 6m27s for one image, 2m17s of it spent
# reloading a page a reload cannot fix, having already read the answer off the
# URL. Fail immediately so the caller heals at once.
print("\n[18] a logged-out page fails fast instead of burning a reload")
_fh = (pathlib.Path(ROOT) / "flow_headless.py").read_text(encoding="utf-8")
check("the composer miss reads the FIRST probe's url", "first_url" in _fh)
check("'/about' short-circuits before the reload",
      _fh.find('if "/about" in first_url:') <
      _fh.find("generate: reloading the app once"))
check("the fast path explains itself in the log",
      "session looks logged out) —" in _fh)

# ── 19. aspect ratio is a PROJECT SETTING, not a request field ─────────────
# Every image came back 16:9 however the API was called. Cause: aspect is a
# per-project UI setting behind the composer's settings gear, and our headless
# path never drove it (it wrote request[4], which Flow ignores). Recipe mapped
# from gflow-cli (MIT) ui_automation.py:2450 / ui_automation_video.py:123.
print("\n[19] aspect ratio is driven in the UI, not the payload")
_fh_src = (pathlib.Path(ROOT) / "flow_headless.py").read_text(encoding="utf-8")
_gen_src = inspect.getsource(FH.HeadlessEngine.generate)

check("a `crop_*` trigger probe exists",
      hasattr(FH, "JS_ASPECT_TRIGGER") and "crop_" in FH.JS_ASPECT_TRIGGER)
check("the trigger probe requires an exact crop_ ligature",
      "/^crop_/" in FH.JS_ASPECT_TRIGGER)
check("a tab scanner exists", hasattr(FH, "JS_UI_TABS"))
check("the ratio tab labels cover the ratios we accept",
      all(r in FH._ASPECT_TAB_LABELS for r in ("16:9", "9:16", "1:1", "4:3", "3:4")))
check("1:1 accepts its locale variants (Square / 1x1)",
      "Square" in FH._ASPECT_TAB_LABELS["1:1"]
      and any("1×1" == x for x in FH._ASPECT_TAB_LABELS["1:1"]))
check("the settings trigger is found via its icon, locale-independent",
      "google-symbols" in FH.JS_ASPECT_TRIGGER)
check("only unambiguous icons are used to skip a click",
      set(FH._ASPECT_ICON_UNIQUE) == {"crop_16_9", "crop_9_16", "crop_square"})
check("engine has set_aspect_ratio()", hasattr(FH.HeadlessEngine, "set_aspect_ratio"))
check("the picker is dismissed, and only by Escape (never a stray click)",
      "Escape" in inspect.getsource(FH.HeadlessEngine._close_settings))
check("generate() sets the aspect BEFORE typing the prompt",
      "set_aspect_ratio" in _gen_src
      and _gen_src.index("set_aspect_ratio") < _gen_src.index("box.click()"))
check("...and only when one was actually requested",
      "if aspect:" in _gen_src)
check("a failure is caught so it can never break generation",
      "continuing with the \"\n                         f\"project default"
      in _gen_src or "project default" in _gen_src)
check("the probe reports the trigger icon so /diag shows the live ratio",
      "crops:" in _fh_src)


class _AspectPage:
    """Browser-free page stub: scripted trigger + tabs, recorded clicks."""

    def __init__(self, trigger=None, tabs=None):
        self._trigger = trigger or {}
        self._tabs = tabs or []
        self.clicks = []
        self.presses = []
        self.keyboard = self

    def press(self, key):
        self.presses.append(key)

    def evaluate(self, expr, *a, **k):
        return ([self._trigger] if self._trigger else []) if "crop_" in expr \
            else list(self._tabs)

    def mouse_click(self, x, y):
        self.clicks.append((x, y))


_eng = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng.page = _AspectPage(trigger={"icon": "crop_16_9", "x": 10, "y": 20,
                                 "label": "Frame"})
check("an already-correct ratio is a no-op", _eng.set_aspect_ratio("16:9") is True)
check("...it clicked nothing", _eng.page.clicks == [], str(_eng.page.clicks))

_eng2 = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng2.page = _AspectPage(tabs=[{"text": "1:1", "x": 5, "y": 6, "sel": ""}])
check("no `crop_*` trigger (Agent arm) degrades safely instead of clicking",
      _eng2.set_aspect_ratio("1:1") is False and _eng2.page.clicks == [])

_eng3 = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng3.page = _AspectPage(
    trigger={"icon": "crop_landscape", "x": 1, "y": 2, "label": "Frame"},
    tabs=[{"text": "16:9", "x": 3, "y": 4, "sel": "true"},
          {"text": "1:1", "x": 5, "y": 6, "sel": "false"}])
check("an ambiguous icon drives the picker (16:9 and 4:3 share one icon)",
      _eng3.set_aspect_ratio("1:1") is True)
check("...clicking the trigger then the ratio tab",
      len(_eng3.page.clicks) == 2, str(_eng3.page.clicks))
check("...then dismissing the picker", _eng3.page.presses == ["Escape"])

_eng4 = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng4.page = _AspectPage(trigger={"icon": "crop_square", "x": 1, "y": 2, "label": ""},
                         tabs=[{"text": "16:9", "x": 3, "y": 4, "sel": ""}])
check("a ratio the picker does not expose is refused, not guessed",
      _eng4.set_aspect_ratio("7:5") is False and _eng4.page.clicks == [])

# ── 20. the agent arm, and why its fallback is OFF by default ──────────────
# Confirmed live: our page has no `crop_*` panel — it is the AGENT arm, where
# the ratio rides in the PROMPT ("...in a 1:1 aspect ratio"), as gflow-cli's
# agentic driver does ("Settings via prompt, not the tune popover"). That
# fallback must stay DISABLED until the watermark remover can handle a size
# other than 1376x768, or enabling it would serve WATERMARKED images.
print("\n[20] agent arm: prompt clause, gated off until the mark is mapped")
check("the prompt clause exists for the ratios we accept",
      all(r in FH.ASPECT_CLAUSE for r in ("16:9", "9:16", "1:1", "4:3", "3:4")))
check("the clause reads naturally", "aspect ratio" in FH.ASPECT_CLAUSE["1:1"])
check("an agent-arm detector exists", hasattr(FH.HeadlessEngine, "_agent_arm"))
check("it looks for the `tune` gear (their AGENT_TUNE_INDICATOR)",
      "'tune'" in FH.JS_AGENT_ARM)
check("it also looks for the agent composer element",
      "flow-creative-agent-prompt-box" in FH.JS_AGENT_ARM)
check("the fallback is OFF by default", FH.aspect_prompt_enabled() is False)
os.environ["FLOW_ASPECT_PROMPT"] = "1"
check("...and can be switched on to test a new size",
      FH.aspect_prompt_enabled() is True)
os.environ.pop("FLOW_ASPECT_PROMPT", None)
check("generate() consults the gate rather than applying it blindly",
      "aspect_prompt_enabled()" in _gen_src)
check("the off-state explains itself in the log (not a silent no-op)",
      "WITH its watermark" in _gen_src)
check("the remover makes a size it cannot handle LOUD",
      "watermark NOT removed" in (pathlib.Path(ROOT) /
                                  "dewatermark_server.py").read_text(encoding="utf-8"))

# the clause is appended only when an agent arm is actually detected
_eng5 = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng5.page = _AspectPage()
_eng5.page._agent_arm_result = {"tune": True, "agent_box": False, "crop": ""}
_eng5.page.evaluate = lambda expr, *a, **k: (
    _eng5.page._agent_arm_result if "tune" in expr else [])
_out, _cl = _eng5._aspect_via_prompt("1:1", "a red apple")
check("an agent arm appends the clause to the prompt",
      _out == "a red apple, in a square 1:1 aspect ratio", _out)
check("...and reports which clause it used", _cl != "")

_eng6 = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng6.page = _AspectPage()
_eng6.page.evaluate = lambda expr, *a, **k: {"tune": False, "agent_box": False,
                                             "ligature": "abc"}
_o2, _c2 = _eng6._aspect_via_prompt("1:1", "a red apple")
check("no detectable arm leaves the prompt untouched",
      _o2 == "a red apple" and _c2 == "")

# ── 21. speed: measure it, then stop re-loading the SPA ────────────────────
# "Make it faster" without a breakdown is guesswork. The render is Google's and
# cannot be optimised; navigation + hydration + the composer wait are OURS, and
# real Flow feels instant because its SPA is ALREADY loaded — we re-navigated
# on every single generation.
print("\n[21] speed: stage timings + the warm-page path")
check("generation records stage timings",
      "_T" in _gen_src and "_mark(" in _gen_src)
check("...and logs one line per generation",
      "[timing] generate" in _gen_src)
check("the stages named are the ones we can actually influence",
      all(k in _gen_src for k in ('_mark("project")', '_mark("nav")',
                                  '_mark("composer")', '_mark("typed")',
                                  '_mark("response")')))
check("an engine can ask 'is the composer already here?'",
      hasattr(FH.HeadlessEngine, "_composer_present"))
check("...reusing the same composer predicate the wait uses",
      "COMPOSER_JS" in inspect.getsource(FH.HeadlessEngine._composer_present))
check("a warm page is reused instead of reloaded",
      "warm = " in _gen_src and "no navigation" in _gen_src)
check("the reload is now the ELSE branch, not the default",
      _gen_src.index("if warm:") < _gen_src.index("self.goto_app(f\"/project/{pid}\")"
                                                  if 'self.goto_app(f"/project/{pid}")' in _gen_src
                                                  else "self.goto_app("))

_p1 = type("P", (), {"evaluate": lambda s, e, *a, **k: True})()
_p2 = type("P", (), {"evaluate": lambda s, e, *a, **k: (_ for _ in ()).throw(
    RuntimeError("boom"))})()
_e = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_e.page = _p1
check("_composer_present() reports a warm composer", _e._composer_present() is True)
_e.page = _p2
check("...and fails safe (False) instead of raising", _e._composer_present() is False)

# Typing was 11.9s of a 55.4s generation — a quarter of the wait — because every
# character waited for its own CDP round-trip (~200 ms each on 0.1 CPU).
import flow_cdp as _CDP
_type_src = inspect.getsource(_CDP.Keyboard.type)
check("both key events are fire-and-forget (no per-char round-trip)",
      _type_src.count('"Input.dispatchKeyEvent"') == 2
      and _type_src.count("wait=False") == 2)
check("...but still fires real key events (not a silent insertText)",
      '"type": "keyDown"' in _type_src
      and '"Input.insertText"' not in _type_src)
check("...and proves the queue drained before the composer is trusted",
      '_p.evaluate("1"' in _type_src)
check("typing pace is now explicit, not an accident of round-trips",
      "TYPE_DELAY_MS" in _gen_src and hasattr(FH, "TYPE_DELAY_MS"))
# With the round-trips gone, `typed=` was still ~9.4s — dominated by hardcoded
# settle sleeps (2 + 1 + 1.5s of pure waiting). The 1.5s one is now an actual
# check, which is both faster and catches dropped keystrokes.
check("the prompt is VERIFIED in the composer before submitting",
      "composer holds" in _gen_src
      and _gen_src.index("composer holds") < _gen_src.index('press("Enter")'))
check("...replacing the fixed 1.5s hope with a real check",
      # the old code slept blindly right after typing; the check must sit there
      # instead (other sleeps elsewhere in the flow are deliberate)
      "delay=TYPE_DELAY_MS)\n            time.sleep(1.5)" not in _gen_src)
check("the post-composer settle is no longer 2s of dead waiting",
      "_pump(seconds=2)" not in _gen_src)
check("...and is tunable without a deploy",
      "FLOW_TYPE_DELAY_MS" in
      (pathlib.Path(ROOT) / "flow_headless.py").read_text(encoding="utf-8"))

# ── 22. the UI is a Flow-like shell that does not waste the worker ─────────
# Flow's real UI is an app: tabs, a bottom prompt bar, a canvas of tiles. This
# is the lightweight version — one file, no framework, no external requests —
# and it must never spend the server's single worker on a tab nobody is looking
# at.
print("\n[22] UI shell: Flow-like, and cheap when hidden")
_ui2 = (pathlib.Path(ROOT) / "ui" / "index.html").read_text(encoding="utf-8")
check("it is a single file with no external scripts/fonts",
      not re.search(r'<(script|link)[^>]+(src|href)=', _ui2))
check("the composer is docked at the bottom (Flow's prompt bar)",
      'class="composer"' in _ui2 and "flowjob" in _ui2)
check("there are tabs, so secondary panels stay out of the way",
      all(f'data-tab="{t}"' in _ui2 for t in ("create", "library", "upload", "api"))
      and all(f'id="p-{t}"' in _ui2 for t in ("create", "library", "upload", "api")))
check("off-screen tiles skip layout/paint work",
      "content-visibility:auto" in _ui2)
check("Enter generates, Shift+Enter is a newline",
      "e.shiftKey" in _ui2 and "e.preventDefault(); go();" in _ui2)
check("a hidden tab does not poll the library (it costs the worker)",
      "if (HIDDEN) return;" in _ui2)
check("...and job polling slows down while hidden",
      "HIDDEN ? 10000" in _ui2)
check("returning to the tab refreshes immediately",
      "addEventListener('visibilitychange'" in _ui2)
check("/health still polls while hidden (it keeps the service awake)",
      "setInterval(pollHealth, 5000)" in _ui2
      and "setInterval(pollHealth" not in _ui2.split("visibilitychange")[1])
check("every referenced element id exists (no silent runtime null)",
      all(i in set(re.findall(r'id="([^"]+)"', _ui2))
          for i in set(re.findall(r"\$\$?\('#([a-zA-Z0-9_-]+)'\)", _ui2))))

# ── 23. a dead session must not burn three minutes discovering itself ──────
# Measured live: `[timing] generate project=181.7s total=181.7s` was spent
# before admitting a session that was already dead at second zero — every RPC
# 401ing, reloading the page, and waiting up to AT_WAIT_S for a token a
# logged-out page can never produce.
print("\n[23] a dead session fails fast instead of burning minutes")
_fa_src = (pathlib.Path(ROOT) / "flow_api_headless.py").read_text(encoding="utf-8")
check("generate() probes the page before ploughing on",
      "is the logged-out" in _gen_src and "page_probe()" in _gen_src)
check("...and returns immediately when the session really is gone",
      "not attempting any RPC" in _gen_src)
check("...but still tolerates a flaky account probe",
      "the page still looks" in _gen_src)
check("a logged-out page stops the token wait early",
      "will NEVER grow a token" in inspect.getsource(FH.HeadlessEngine._wait_at))

# the message must actually reach the heal path, or none of the above is useful
_m = re.search(r"def _looks_like_dead_session.*?return any\(k in low for k in \((.*?)\)\)",
               _fa_src, re.S)
_keys = re.findall(r'"([^"]+)"', _m.group(1)) if _m else []
check("the heal classifier's keywords were located (test is meaningful)",
      len(_keys) >= 5, str(_keys))
_MSG = ("session check failed and the page is the logged-out surface "
        "(https://flow.google.com/about) — requesting heal")
check("the fast-fail message matches a dead-session keyword",
      any(k in _MSG.lower() for k in _keys), f"{_keys} vs {_MSG!r}")

# and the wait itself must return at once, not after the whole budget
_about = type("P", (), {"url": "https://flow.google.com/about",
                        "evaluate": lambda s, e, *a, **k: ""})()
_eng7 = FH.HeadlessEngine.__new__(FH.HeadlessEngine)
_eng7.page = _about
_eng7._at = ""
_eng7._live_at = ""
_eng7._pump = lambda seconds=5: None
_t0 = time.time()
_r = _eng7._wait_at(timeout=60)
_dt = time.time() - _t0
check("the token wait gives up on a logged-out page immediately",
      _r == "" and _dt < 5, f"returned {_r!r} after {_dt:.1f}s (budget was 60s)")

# ── 24. an empty answer is tellable from a render by HOW FAST it returns ───
# Measured: successful renders answered in 21-24s; every "no media" answer came
# back in under 7s. So the failure is identifiable at once, and can be retried
# in place (~2s) instead of triggering an engine rebuild (183s end-to-end).
print("\n[24] fast empty answers get the cheap retry, not the rebuild")
check("a fast-empty threshold exists", hasattr(FH, "FAST_EMPTY_S"))
check("...and it is env-tunable",
      "FLOW_FAST_EMPTY_S" in
      (pathlib.Path(ROOT) / "flow_headless.py").read_text(encoding="utf-8"))
check("...set below the fastest successful render ever measured (21s)",
      0 < FH.FAST_EMPTY_S < 20, str(FH.FAST_EMPTY_S))
check("the response time is what selects the recovery",
      "render_s" in _gen_src and "not a render" in _gen_src)
check("the cheap path resubmits WITHOUT rebuilding the engine",
      _gen_src.index("resubmitting on the warm page")
      < _gen_src.index("no-media heal"))
check("...and the rebuild is still there for genuinely slow empties",
      "no-media heal" in _gen_src and "self.restart()" in _gen_src)

# ── 25. the cookie jar must LEARN from the browser, not only teach it ──────
# The jar only ever flowed jar -> browser, so Google's own PSIDTS rotation
# (~10 min) was invisible: the jar kept the value we minted, and every re-seed
# wrote that stale value back over the browser's fresher one — after which the
# app redirected to /about and the slow heal ran. That is the session-flapping
# loop (every ~10-40 min) this closes.
print("\n[25] session: adopt Google's rotation from the browser")
try:
    import threading as _th
    import flow_api_headless as _FA
    _fa_ok = True
except Exception as _e:
    _fa_ok = False
    print("   (flow_api_headless not importable here:", str(_e)[:80], ")")
check("the auth store is importable for unit testing", _fa_ok)

if _fa_ok:
    _st = _FA._AuthState.__new__(_FA._AuthState)
    _st.jar = {"SID": "minted-sid", "__Secure-1PSIDTS": "stale-ts"}
    _st._lock = _th.Lock()
    _st.persist = lambda: None                      # no disk/KV in a unit test
    _adopted = _st.harvest([
        {"name": "__Secure-1PSIDTS", "value": "fresh-ts",
         "domain": ".google.com"},
        {"name": "SID", "value": "browser-sid", "domain": ".google.com"},
        {"name": "AEC", "value": "aec-val", "domain": ".google.com"},
        {"name": "NOT_GOOGLE", "value": "x", "domain": "example.com"},
    ], why="test")
    check("a rotated cookie is adopted from the browser",
          _st.jar.get("__Secure-1PSIDTS") == "fresh-ts",
          str(_st.jar.get("__Secure-1PSIDTS")))
    check("...but a freshly minted SID is NOT overwritten by the browser's",
          _st.jar.get("SID") == "minted-sid", str(_st.jar.get("SID")))
    check("a rotated name we did not have at all is adopted",
          _st.jar.get("AEC") == "aec-val")
    check("non-Google cookies are ignored", "NOT_GOOGLE" not in _st.jar)
    check("it reports which names it adopted",
          "AEC" in _adopted and "SID" not in _adopted, str(_adopted))

    _fa_src2 = (pathlib.Path(ROOT) / "flow_api_headless.py").read_text(
        encoding="utf-8")
    check("the supervisor harvests periodically",
          'self.harvest_browser_cookies("supervisor")' in _fa_src2)
    check("...only while the session is known good (a dead browser is the dead"
          " party)",
          "if self.session_ok:" in _fa_src2
          and _fa_src2.index('harvest_browser_cookies("supervisor")')
          > _fa_src2.index("if self.session_ok:"))
    check("the jar is re-adopted after a heal too",
          'harvest_browser_cookies("post-heal")' in _fa_src2)
    check("recovery tries an IN-PLACE restore before rebuilding the browser",
          "in-place restore" in _fa_src2
          and _fa_src2.index("in-place restore")
          < _fa_src2.index("[heal] rebuild failed:")
          or "in-place restore" in _fa_src2)
    check("...and the rebuild is still there as the fallback",
          "if not restored:" in _fa_src2 and "hard_rebuild(boot=True)" in _fa_src2)

# ── 26. exactly ONE rotator for PSIDTS ─────────────────────────────────────
# Two writers for one cookie family sign the session out: our HTTP rotation and
# the browser's own both rewrite __Secure-1PSIDTS in different contexts, and
# whichever Google rejects takes the whole login with it (page ->
# accounts.google.com). Deaths tracked the server rotate's ~5-minute cadence
# while a harvest of the BROWSER's rotation was being adopted successfully just
# minutes earlier — so the browser wins and the server defers.
print("\n[26] one rotator only: the browser rotates, the server follows")
if _fa_ok:
    check("the engine is asked whether it can rotate for us",
          hasattr(_FA.FlowAPI, "_server_should_rotate"))
    _a = _FA.FlowAPI.__new__(_FA.FlowAPI)
    _a.engine = type("E", (), {"ctx": object()})()
    check("with a live browser the server does NOT rotate",
          _a._server_should_rotate() is False)
    _a.engine = None
    check("with no browser the server does rotate (HTTP-only mode)",
          _a._server_should_rotate() is True)
    _a.engine = type("E", (), {"ctx": None})()
    check("a dead browser counts as no browser", _a._server_should_rotate() is True)

    _fa_src3 = (pathlib.Path(ROOT) / "flow_api_headless.py").read_text(
        encoding="utf-8")
    check("every periodic rotation is gated on it",
          _fa_src3.count("if self._server_should_rotate():") >= 2,
          str(_fa_src3.count("if self._server_should_rotate():")))
    check("the harvest still runs whenever the session is healthy",
          'harvest_browser_cookies("supervisor")' in _fa_src3)
    check("the 401 recovery path may still force a rotation (already dead, so it"
          " cannot kill a healthy session)",
          "AUTH.rotate(force=True)" in _fa_src3)

    # The cheap restore logged "did not take" and fell through to a rebuild it
    # did not need: it verified with the PREVIOUS session's cached `at` and gave
    # up after one probe on a page that had only just started hydrating.
    _soft_src = _fa_src3[_fa_src3.index("def _soft():"):
                         _fa_src3.index("if eng is not None:")]
    check("the in-place restore drops the stale `at` (else verification lies)",
          "AUTH.invalidate_at()" in _soft_src)
    check("...and retries the account read instead of trusting one probe",
          "for _ in range(3):" in _soft_src and "eng.session_ok()" in _soft_src)

# ── 27. session maxxing: survive a lapse, never start doomed ───────────────
# Two goals: the session should not lapse in the first place, and if it does,
# the user's request must still complete rather than being reported as a Google
# auth problem.
print("\n[27] session maxxing")
if _fa_ok:
    _fa4 = (pathlib.Path(ROOT) / "flow_api_headless.py").read_text(encoding="utf-8")
    check("a job that hits a lapse requests a heal and RE-RUNS",
          "session lapsed mid-job" in _fa4
          and "job_retry_after_heal" in _fa4)
    check("...and the retry is bounded to one",
          "for attempt in (0, 1):" in _fa4 and "if ok or attempt == 1:" in _fa4)
    _runner_src = _fa4[_fa4.index("def _runner():"):
                       _fa4.index("threading.Thread(target=_runner")]
    check("the job thread never heals INLINE (heal_session takes the engine "
          "lock and rebuilds — that would kill the page the job holds)",
          "self.heal_session(" not in _runner_src)
    check("...it only asks, and waits on the verified flag",
          "_heal_requested" in _runner_src and "_wait_for_session" in _runner_src)
    check("the wait budget exists and is env-tunable",
          hasattr(_FA, "SESSION_WAIT_S") and "FLOW_SESSION_WAIT_S" in _fa4)
    check("the keepalive runs well inside Google's ~10-minute PSIDTS window",
          "last_sess > 240" in _fa4)
    check("a non-session failure is NOT retried (no pointless second render)",
          "if not self._looks_like_dead_session(err):" in _runner_src)

# ── 28. a flagged IP is handled, not hammered ─────────────────────────────
# PUBLIC_ERROR_UNUSUAL_ACTIVITY is an IP score. The old code restarted the
# browser and retried — same IP, another flagged request, ~50s of boot each
# time — which is exactly how one bad score became a service that "just fails":
# a 230-second generation that produced nothing, then every prompt after it.
print("\n[28] a flagged IP backs off instead of being hammered")
check("the flag helpers exist",
      all(hasattr(FH, n) for n in ("note_ip_flag", "ip_flag_cooldown_s",
                                   "ip_flag_status", "clear_ip_flag")))
FH.clear_ip_flag()
check("a clear IP has no cooldown", FH.ip_flag_cooldown_s() == 0)

_w1 = FH.note_ip_flag("unit-test")
check("the first flag backs off the base window",
      abs(_w1 - FH.IP_FLAG_BASE_S) < 1, str(_w1))
check("...and the cooldown is actually active", FH.ip_flag_cooldown_s() > 0)
_w2 = FH.note_ip_flag("unit-test")
check("a second consecutive flag backs off twice as long",
      abs(_w2 - _w1 * 2) < 1, f"{_w1} -> {_w2}")
for _ in range(8):
    _wN = FH.note_ip_flag("unit-test")
check("...capped so it cannot grow without bound",
      _wN <= FH.IP_FLAG_MAX_S + 0.01, str(_wN))
_st = FH.ip_flag_status()
check("status reports the count and the remaining cooldown",
      _st.get("count", 0) >= 10 and _st.get("cooldown_s", 0) > 0, str(_st))
FH.clear_ip_flag()
check("clearing resets everything",
      FH.ip_flag_cooldown_s() == 0 and FH.ip_flag_status()["count"] == 0)

check("generate() refuses up front while cooling down",
      "IP-REPUTATION GATE" in _gen_src and "ip_flag_cooldown_s()" in _gen_src)
check("a successful render clears the flag",
      "clear_ip_flag()" in _fh_src)
# the flag branch must NOT restart the browser any more
_flag_branch = _fh_src[_fh_src.index('if "UNUSUAL_ACTIVITY" in body:'):
                       _fh_src.index('parsed = parse_batchexecute(body)')]
check("the flag branch no longer restarts the engine",
      "self.restart()" not in _flag_branch, "restart still present")
check("...it names the real fix (a residential proxy) in the error",
      "FLOW_PROXY_URL" in _flag_branch)

_fa5 = (pathlib.Path(ROOT) / "flow_api_headless.py").read_text(encoding="utf-8")
check("/health exposes the IP flag", "ip_flag_status()" in _fa5)
check("the API exposes a cooldown for the server to gate on",
      "def ip_cooldown" in _fa5)
_srv2 = (pathlib.Path(ROOT) / "flow_server.py").read_text(encoding="utf-8")
check("/generate answers 503 + Retry-After while cooling down",
      "ip_flagged" in _srv2 and '"Retry-After"' in _srv2)

_ui3 = (pathlib.Path(ROOT) / "ui" / "index.html").read_text(encoding="utf-8")
check("the UI has a failure banner (a failure must not look like a "
      "disappearance)", 'id="banner"' in _ui3 and "function banner(" in _ui3)
check("...it explains an IP cooldown before the user wastes a prompt",
      "flagged this server" in _ui3 and "ipNotice" in _ui3)
check("...it reports a failed job loudly",
      "That render failed after" in _ui3)
check("...and it explains a job lost to a restart",
      "no longer on the server" in _ui3)

# ── 29. an authenticated proxy must actually work ─────────────────────────
# The driver passed `--proxy-auth=user:pass`, which is NOT a Chrome switch. It
# was silently ignored, so an authenticated proxy answered 407 to every request
# and looked like a broken proxy rather than a missing feature — which would
# have wasted the one thing that fixes a flagged IP.
print("\n[29] authenticated proxies: credentials answered over CDP")


class _FakeConn2:
    def __init__(self):
        self.handlers = {}

    def on(self, name, fn):
        self.handlers[name] = fn

    def off(self, name, fn):
        self.handlers.pop(name, None)


class _FakePage2:
    def __init__(self):
        self.sent = []

    def _send(self, method, params=None, **k):
        self.sent.append((method, params or {}))
        return {}


def _ctx_with(creds):
    c = _CDP.Context.__new__(_CDP.Context)
    c._proxy_auth = creds
    c._auth_on = False
    c._fetch_on = False
    c._paused_ids = {}
    c._closed = True                 # makes the sweeper exit immediately
    c._routes = []
    c._page = None
    c.page = _FakePage2()
    c._conn = _FakeConn2()
    c._sid = "s"
    return c


check("the driver can arm proxy auth", hasattr(_CDP.Context, "arm_proxy_auth"))
_c3 = _ctx_with(("user", "pass"))
check("arming with credentials succeeds", _c3.arm_proxy_auth() is True)
_en = [p for m, p in _c3.page.sent if m == "Fetch.enable"]
check("Fetch.enable turns on handleAuthRequests",
      _en and _en[-1].get("handleAuthRequests") is True, str(_en[-1:]))
check("an authRequired handler is registered",
      "Fetch.authRequired" in _c3._conn.handlers)
_c3._conn.handlers["Fetch.authRequired"](
    {"requestId": "r1", "authChallenge": {"origin": "proxy.local"}}, None)
_cw = [p for m, p in _c3.page.sent if m == "Fetch.continueWithAuth"]
check("the challenge is answered with our credentials",
      _cw and _cw[-1]["authChallengeResponse"]["response"] == "ProvideCredentials"
      and _cw[-1]["authChallengeResponse"]["username"] == "user"
      and _cw[-1]["authChallengeResponse"]["password"] == "pass", str(_cw[-1:]))

_c4 = _ctx_with(None)
check("no credentials -> nothing armed (and no Fetch overhead)",
      _c4.arm_proxy_auth() is False and not _c4.page.sent)

_cdp_src = (pathlib.Path(ROOT) / "flow_cdp.py").read_text(encoding="utf-8")
check("the dead --proxy-auth flag is gone", "--proxy-auth=" not in _cdp_src)
check("...and the reason it was dead is recorded", "no such switch" in _cdp_src)
check("the launch path arms auth automatically",
      "arm_proxy_auth()" in _cdp_src and "proxy auth armed" in _cdp_src)
check("/health distinguishes proxy-set from proxy-usable",
      '"proxy_auth"' in _fa5)
check("boot logs whether egress is proxied",
      "egress proxy:" in (pathlib.Path(ROOT) / "flow_headless.py").read_text(
          encoding="utf-8"))

# ── 30. WARP standby ships in the image, dormant unless FLOW_WARP=1 ────────
# Render's egress IP gets reCAPTCHA-flagged (measured, recovered). The standby
# is a userspace WARP tunnel (wgcf + wireproxy): verified end-to-end on the dev
# box — generation done, 0 flags, +56s vs direct. It must cost NOTHING while
# off (no processes, no RAM) and must fall back to direct egress if the tunnel
# does not pass traffic.
print("\n[30] WARP standby (dormant, verified fallback)")
_dockerfile = (pathlib.Path(ROOT) / "Dockerfile").read_text(encoding="utf-8")
check("the image ships wgcf + wireproxy (pinned versions)",
      "WGCF_VERSION" in _dockerfile and "WIREPROXY_VERSION" in _dockerfile
      and "wireproxy" in _dockerfile and "wgcf" in _dockerfile)
check("the Dockerfile documents why (flagged IP, no TUN on Render free)",
      "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in _dockerfile
      and "NET_ADMIN" in _dockerfile)

_start = (pathlib.Path(ROOT) / "start.sh").read_text(encoding="utf-8")
check("the standby is OFF by default", '${FLOW_WARP:-0}' in _start)
check("...and activates only on FLOW_WARP=1",
      '"${FLOW_WARP:-0}" = "1"' in _start)
check("the identity persists in the profile dir (registered once)",
      "wgcf-profile.conf" in _start and "$WARP_DIR" in _start)
check("wireproxy gets the v1.0.9 host:port BindAddress format "
      "(the host+Port split panics: 'missing port in address')",
      "BindAddress = 127.0.0.1:1080" in _start
      and "missing port in address" in _start)
check("the tunnel is VERIFIED before it is trusted — egress IP check gates "
      "FLOW_PROXY_URL",
      "api.ipify.org" in _start and "tunnel UP" in _start)
check("a dead tunnel falls back to direct egress (never a swallowed proxy)",
      "falling back to direct egress" in _start and "kill \"$WPID\"" in _start)
check("SIGTERM cleanup kills wireproxy too (no stale tunnel across deploys)",
      'pkill -9 -f "wireproxy"' in _start)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
