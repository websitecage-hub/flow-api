"""CRUX TEST: can chrome-headless-shell mint a reCAPTCHA Enterprise token for
the Flow site key with the GENERATE action — from the flow.google.com origin?

If yes: a browser path inside 512MB is real. If no: the gate is genuine for
this runtime too (and the repo's conclusion holds even for tiny browsers).
"""
import glob, json, os, sys, time
from playwright.sync_api import sync_playwright

SITE = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
MODE = sys.argv[1] if len(sys.argv) > 1 else "shell"   # shell | full

if MODE == "shell":
    exe = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))[-1]
else:
    exe = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))[-1]
print("MODE:", MODE, "exe:", exe)
prof = f"/tmp/crux-{MODE}"
os.system(f"rm -rf {prof}")

INJECT = """(site) => {
  return new Promise((resolve) => {
    window.__tok = null; window.__err = null;
    if (window.grecaptcha && window.grecaptcha.enterprise) { resolve('already'); return; }
    const s = document.createElement('script');
    s.src = 'https://www.google.com/recaptcha/enterprise.js?render=' + site;
    s.onload = () => resolve('loaded');
    s.onerror = () => resolve('script_error');
    document.head.appendChild(s);
  });
}"""

EXEC = """(site) => {
  return new Promise((resolve) => {
    try {
      if (!window.grecaptcha || !window.grecaptcha.enterprise) { resolve({ok:false, err:'no grecaptcha'}); return; }
      window.grecaptcha.enterprise.ready(() => {
        const t0 = Date.now();
        window.grecaptcha.enterprise.execute(site, {action: 'GENERATE'})
          .then(tok => resolve({ok:true, ms: Date.now()-t0, len:(tok||'').length, prefix:(tok||'').slice(0,6)}))
          .catch(e => resolve({ok:false, err: String(e).slice(0,300), ms: Date.now()-t0}));
      });
      setTimeout(() => resolve({ok:false, err:'timeout 45s'}), 45000);
    } catch(e) { resolve({ok:false, err: String(e).slice(0,300)}); }
  });
}"""

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        prof, executable_path=exe, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        ignore_https_errors=True, bypass_csp=True)
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("https://flow.google.com/", wait_until="commit", timeout=60000)
    for i in range(20):
        try: pg.evaluate("1")
        except Exception: pass
        time.sleep(0.5)
    print("origin:", pg.evaluate("location.origin"))
    st = pg.evaluate(INJECT, SITE)
    print("inject:", st)
    time.sleep(5)
    for i in range(10):
        try: pg.evaluate("1")
        except Exception: pass
        time.sleep(0.5)
    res = pg.evaluate(EXEC, SITE)
    print("EXECUTE RESULT:", json.dumps(res))
    try:
        info = pg.evaluate("""() => ({
          cfg: Object.keys(window.___grecaptcha_cfg || {}),
          ent: !!(window.grecaptcha && window.grecaptcha.enterprise),
          clients: (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) ? Object.keys(window.___grecaptcha_cfg.clients).length : 0
        })""")
        print("grecaptcha state:", json.dumps(info))
    except Exception as e:
        print("state err", str(e)[:200])
    ctx.close()
print("DONE")