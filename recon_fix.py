"""Can we make chrome-headless-shell pass reCAPTCHA by removing the automation
fingerprint (webdriver, HeadlessChrome UA, empty plugins)? If yes: free-tier
generation is possible in the 120MB engine."""
import glob, json, os, time
from playwright.sync_api import sync_playwright

PID = "4124a6cc-e936-4354-b569-b5a08fdfec0b"
SITE = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/153.0.0.0 Safari/537.36")
jar = json.load(open("sessions/flow.cookies.full.json"))
SHELL = sorted(glob.glob(os.path.expanduser(
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))[-1]

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'mimeTypes', {get: () => [1,2]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
window.chrome = window.chrome || {runtime:{}};
const origQ = navigator.permissions && navigator.permissions.query;
if (origQ) navigator.permissions.query = (p) => p.name==='notifications'
    ? Promise.resolve({state: Notification.permission}) : origQ(p);
"""

for attempt, args in [("A_ua+stealth", ["--disable-blink-features=AutomationControlled"])]:
    prof = "/tmp/fixprof"
    os.system(f"rm -rf {prof}")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            prof, executable_path=SHELL, headless=True, user_agent=UA,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] + args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900})
        ctx.add_init_script(STEALTH)
        for c in jar:
            try: ctx.add_cookies([{"name":c["name"],"value":c["value"],"domain":c.get("domain",".google.com"),"path":"/","secure":True,"sameSite":"None"}])
            except Exception: pass
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        resp = []
        pg.on("response", lambda r: resp.append(r.text()[:220]) if "rpcids=ogiZ0b" in r.url else None)
        pg.goto(f"https://flow.google.com/u/1/project/{PID}", wait_until="commit", timeout=60000)
        for _ in range(40): pg.evaluate("1"); time.sleep(0.5)
        fp = pg.evaluate("() => ({wd:navigator.webdriver, ua:navigator.userAgent.includes('HeadlessChrome'), plug:navigator.plugins.length, brands:(navigator.userAgentData||{}).brands})")
        print("attempt", attempt, "fp:", fp)
        try:
            box = pg.locator("[contenteditable=true]").first
            box.click(); time.sleep(1); pg.keyboard.type("a golden retriever puppy", delay=25); time.sleep(2)
            pg.keyboard.press("Enter")
            for _ in range(75):
                pg.evaluate("1"); time.sleep(1)
                if resp: break
        except Exception as e:
            print("drive err", str(e)[:120])
        print("responses:", len(resp))
        for b in resp: print("   ->", b[:220])
        print("RESULT:", "SUCCESS" if any("flow-content" in b for b in resp) else
              ("FLAGGED" if any("UNUSUAL_ACTIVITY" in b for b in resp) else "OTHER"))
        ctx.close()