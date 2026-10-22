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

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
sys.exit(1 if FAIL else 0)
