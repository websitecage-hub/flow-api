"""Recon (not UI automation): load flow.google.com logged-out, record EVERY
network request, save all JS module chunk URLs the app pulls in."""
import glob, json, os, time
from playwright.sync_api import sync_playwright

out = {"urls": [], "js": []}
cands = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
chrome = sorted(cands)[-1]
prof = "/tmp/reconprof"
os.system(f"rm -rf {prof}")

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        prof, executable_path=chrome, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    seen = []
    pg.on("request", lambda r: seen.append((r.resource_type, r.url)))
    pg.goto("https://flow.google.com/", wait_until="commit", timeout=60000)
    # pump JS + wait long enough for lazy chunks to load
    for i in range(40):
        try: pg.evaluate("1")
        except Exception: pass
        time.sleep(0.7)
    time.sleep(10)
    try:
        # scroll to trigger more lazy loads
        pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(5)
    except Exception: pass
    out["urls"] = [{"type": t, "url": u} for t, u in seen]
    out["title"] = pg.title()
    ctx.close()

os.makedirs("recon", exist_ok=True)
js = sorted({u for _, u in seen if ".js" in u.split("?")[0]})
out["js"] = js
json.dump(out, open("recon/requests.json", "w"), indent=1)
print("total requests:", len(seen))
print("js files:", len(js))
for u in js:
    print("  ", u[:150])
print("--- gstatic module chunks ---")
for u in js:
    if "gstatic.com/_/mss" in u or "/js/" in u:
        print("  CHUNK", u[:170])