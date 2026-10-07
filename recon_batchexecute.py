"""Spike: fire a real Flow batchexecute RPC from INSIDE the page context
(real network stack + cookies + origin), with a freshly minted reCAPTCHA
Enterprise token. This is 'no UI clicking' — the genuine API request.

Uses chrome-headless-shell (115MB). Reads the raw server response so we can
see exactly what the backend accepts/rejects.
"""
import glob, json, os, time, urllib.parse
from playwright.sync_api import sync_playwright

SITE = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
exe = sorted(glob.glob(os.path.expanduser(
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))[-1]
prof = "/tmp/spikeprof"
os.system(f"rm -rf {prof}")

MINT = """(site) => new Promise((res) => {
  const done = (o) => res(o);
  const go = () => window.grecaptcha.enterprise.ready(() =>
      window.grecaptcha.enterprise.execute(site, {action:'GENERATE'})
        .then(t => done({ok:true, token:t})).catch(e => done({ok:false, err:String(e)})));
  if (window.grecaptcha && window.grecaptcha.enterprise) return go();
  const s=document.createElement('script');
  s.src='https://www.google.com/recaptcha/enterprise.js?render='+site;
  s.onload=go; s.onerror=()=>done({ok:false,err:'script_err'});
  document.head.appendChild(s);
  setTimeout(()=>done({ok:false,err:'timeout'}), 40000);
})"""

# fire the RPC from inside the page
CALL = """async ({rpcid, payload, at, sourcePath}) => {
  const fReq = JSON.stringify([[[rpcid, JSON.stringify(payload), null, "generic"]]]);
  const body = new URLSearchParams();
  body.set('f.req', fReq); body.set('at', at || '');
  const url = `${location.origin}/u/1/_/AiSandboxAngularFrontend/data/batchexecute?rpcids=${rpcid}&source-path=${encodeURIComponent(sourcePath)}`;
  try {
    const r = await fetch(url, {
      method:'POST', credentials:'include',
      headers:{'content-type':'application/x-www-form-urlencoded;charset=UTF-8'},
      body: body.toString()
    });
    const text = await r.text();
    return {status: r.status, len: text.length, head: text.slice(0, 1200)};
  } catch(e) { return {status:-1, err: String(e)}; }
}"""

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        prof, executable_path=exe, headless=True,
        args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"],
        bypass_csp=True, viewport={"width":1280,"height":900})
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("https://flow.google.com/", wait_until="commit", timeout=60000)
    for i in range(16):
        try: pg.evaluate("1")
        except Exception: pass
        time.sleep(0.5)

    # 1) mint token
    m = pg.evaluate(MINT, SITE)
    print("MINT:", {k:(v[:12]+'...' if k=='token' else v) for k,v in (m or {}).items()})
    token = (m or {}).get("token","")

    # 2) scrape 'at' token from page HTML (and from any inline state)
    at = pg.evaluate("""() => {
      const h = document.documentElement.innerHTML;
      const m = h.match(/AIQ-[A-Za-z0-9_\\-]{20,80}:\\d{10,20}/);
      return m ? m[0] : '';
    }""")
    print("AT token from DOM:", at[:40] or "(none)")
    # also try app config globals
    cfgkeys = pg.evaluate("""() => {
      const out=[]; for (const k in window){ try{ if(/AIQ-/.test(String(window[k]).slice(0,200))) out.push(k);}catch(e){} }
      return out.slice(0,10);
    }""")
    print("globals with AIQ:", cfgkeys)

    # 3) fire reads
    for rpcid, payload, sp in [
        ("nzlxg", [], "/u/1/project/none"),
        ("cPZSdc", [], "/u/1/project/none"),
    ]:
        r = pg.evaluate(CALL, {"rpcid":rpcid,"payload":payload,"at":at,"sourcePath":sp})
        print(f"\n=== {rpcid} -> {r}")
    ctx.close()
print("DONE")