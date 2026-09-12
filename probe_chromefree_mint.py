"""Quick chrome-free reCAPTCHA Enterprise mint probe.
1) Fetch enterprise.js, map reload/anchor endpoints.
2) Attempt a pure-HTTP (curl_cffi chrome146) token mint with current cookies.
3) Compare against a known-good UI token shape from captured traffic.
"""
import re, json, urllib.parse
from pathlib import Path
from curl_cffi import requests as cffi
SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
out = {"site_key": SITE_KEY}
s = cffi.Session(impersonate="chrome146")
try:
    cook = json.loads(Path("sessions/flow.cookies.full.json").read_text(encoding="utf-8"))
    for c in cook:
        dom = c.get("domain", "")
        if dom.endswith("google.com"):
            try:
                s.cookies.set(c["name"], c["value"], domain=dom, path=c.get("path", "/"))
            except Exception:
                pass
    out["cookies_loaded"] = len(cook)
except Exception as e:
    out["cookies_loaded"] = f"err {e}"
# 1) enterprise.js loader
r = s.get(f"https://www.google.com/recaptcha/enterprise.js?render={SITE_KEY}", timeout=30)
out["loader_status"] = r.status_code
out["loader_len"] = len(r.content)
out["loader_snip"] = r.text[:400].replace("\n", " ")
# find the real JS URL
m = re.search(r'src="([^"]*recaptcha[^"]*\.js[^"]*)"', r.text)
real = m.group(1) if m else ""
if real.startswith("/"):
    real = "https://www.google.com" + real
out["real_js"] = real[:200]
js_endpoints = []
if real:
    r2 = s.get(real, timeout=30)
    out["real_js_status"] = r2.status_code
    out["real_js_len"] = len(r2.content)
    t = r2.text
    for pat in [r"/recaptcha/enterprise/(reload|anchor|bframe|webworker|api2/[a-z]+)[^\"' ]*",
                r"enterprise/execute[^\"' ]*", r"2fa[^\"']{0,40}", r"risk[^\"']{0,40}"]:
        js_endpoints += re.findall(pat, t)[:8]
    out["js_endpoint_hits"] = sorted(set(js_endpoints))[:20]
    out["has_execute"] = "enterprise.execute" in t or "execute" in t[:100000]
# 2) direct reload attempt (what the JS would call after heavy client-side work)
reload_url = f"https://www.google.com/recaptcha/enterprise/reload?k={SITE_KEY}"
r3 = s.get(reload_url, timeout=30, headers={"Referer": "https://flow.google.com/"})
out["reload_get_status"] = r3.status_code
out["reload_get_snip"] = r3.text[:300].replace("\n", " ")
# 3) UI token shape from last known-good capture
try:
    cap = json.loads(Path("reports/captured_generation.json").read_text(encoding="utf-8"))
    gen = next(c for c in cap if c.get("postData") and "batchGenerateImages" in c["url"])
    tok = json.loads(gen["postData"])["clientContext"]["recaptchaContext"]["token"]
    out["ui_token_len"] = len(tok)
    out["ui_token_prefix"] = tok[:20]
except Exception as e:
    out["ui_token_err"] = str(e)[:150]
Path("reports/chromefree_mint_probe.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
print(json.dumps(out, indent=2)[:3000], flush=True)
