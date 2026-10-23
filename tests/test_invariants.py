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
import sys

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
check("liveness probe uses a short timeout (25s, not 60s)",
      "timeout=25" in _sup and "timeout=60" not in _sup)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
