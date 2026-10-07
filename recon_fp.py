"""Why does full multi-process Chrome pass reCAPTCHA while headless-shell and
--single-process fail? Collect the SAME fingerprint signals in each mode and
diff them. Plus mint a token in each to compare token quality."""
import glob, json, os, sys, time
from playwright.sync_api import sync_playwright

PID = "4124a6cc-e936-4354-b569-b5a08fdfec0b"
SITE = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
jar = json.load(open("sessions/flow.cookies.full.json"))

FULL = sorted(glob.glob(os.path.expanduser(
    "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))[-1]
SHELL = sorted(glob.glob(os.path.expanduser(
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")))[-1]

PROBE = r"""() => {
  const r = {};
  const n = navigator;
  r.ua = n.userAgent;
  r.webdriver = n.webdriver;
  r.cores = n.hardwareConcurrency;
  r.devMem = n.deviceMemory;
  r.platform = n.platform;
  r.langs = (n.languages||[]).join(',');
  r.plugins = n.plugins ? n.plugins.length : -1;
  r.mimeTypes = n.mimeTypes ? n.mimeTypes.length : -1;
  r.hasChrome = !!window.chrome;
  r.chromeKeys = window.chrome ? Object.keys(window.chrome).slice(0,15) : null;
  r.hasChromeRuntime = !!(window.chrome && window.chrome.runtime);
  r.uaData = n.userAgentData ? {brands:(n.userAgentData.brands||[]).map(b=>b.brand+':'+b.version),
                                 mobile:n.userAgentData.mobile, platform:n.userAgentData.platform}
                             : null;
  r.permissions = !!n.permissions;
  r.screen = [screen.width, screen.height, screen.colorDepth];
  r.dpr = window.devicePixelRatio;
  r.tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  // canvas
  try { const c=document.createElement('canvas'); r.canvas2d = !!c.getContext('2d'); } catch(e){ r.canvas2d='ERR'; }
  // webgl
  try {
    const c=document.createElement('canvas');
    const gl=c.getContext('webgl')||c.getContext('experimental-webgl');
    if (gl) {
      r.webgl = true;
      const dbg=gl.getExtension('WEBGL_debug_renderer_info');
      r.glVendor = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR);
      r.glRenderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
      r.glVersion = gl.getParameter(gl.VERSION);
    } else { r.webgl = false; }
  } catch(e){ r.webgl = 'ERR '+String(e).slice(0,60); }
  // webgl2
  try { const c=document.createElement('canvas'); r.webgl2 = !!c.getContext('webgl2'); } catch(e){ r.webgl2='ERR'; }
  // audio
  try { const actx = new (window.AudioContext||window.webkitAudioContext)(); r.audio = actx.sampleRate; actx.close(); } catch(e){ r.audio='ERR'; }
  r.grecaptcha = !!(window.grecaptcha && window.grecaptcha.enterprise);
  r.acfg = window.___grecaptcha_cfg ? Object.keys(window.___grecaptcha_cfg) : null;
  return r;
}"""

MINT = """(site) => new Promise((res) => {
  const fin=(o)=>res(o);
  const go=()=>window.grecaptcha.enterprise.ready(()=>{
    const t0=Date.now();
    window.grecaptcha.enterprise.execute(site,{action:'IMAGE_GENERATION'})
      .then(t=>fin({ok:true, ms:Date.now()-t0, len:(t||'').length, prefix:(t||'').slice(0,8)}))
      .catch(e=>fin({ok:false, err:String(e).slice(0,150)}));
  });
  if(window.grecaptcha&&window.grecaptcha.enterprise) return go();
  const s=document.createElement('script');
  s.src='https://www.google.com/recaptcha/enterprise.js?render='+site;
  s.onload=go; s.onerror=()=>fin({ok:false,err:'script_err'});
  document.head.appendChild(s);
  setTimeout(()=>fin({ok:false,err:'timeout'}),45000);
})"""

def run(label, exe, extra_args):
    prof = f"/tmp/probe-{label}"
    os.system(f"rm -rf {prof}")
    out = {"mode": label, "exe": os.path.basename(exe)}
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                prof, executable_path=exe, headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                     + extra_args,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 900}, bypass_csp=True)
            for c in jar:
                try:
                    ctx.add_cookies([{"name": c["name"], "value": c["value"],
                                      "domain": c.get("domain", ".google.com"),
                                      "path": "/", "secure": True,
                                      "sameSite": "None"}])
                except Exception:
                    pass
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(f"https://flow.google.com/u/1/project/{PID}",
                    wait_until="commit", timeout=60000)
            for _ in range(40):
                try: pg.evaluate("1")
                except Exception: pass
                time.sleep(0.5)
            out["fp"] = pg.evaluate(PROBE)
            out["mint"] = pg.evaluate(MINT, SITE)
            # composer result
            resp = []
            pg.on("response", lambda r: resp.append(r.text()[:200])
                  if "rpcids=ogiZ0b" in r.url else None)
            try:
                box = pg.locator("[contenteditable=true]").first
                if box.count():
                    box.click(); time.sleep(1)
                    pg.keyboard.type("a test fox", delay=25); time.sleep(2)
                    pg.keyboard.press("Enter")
                    for _ in range(70):
                        try: pg.evaluate("1")
                        except Exception: pass
                        time.sleep(1)
                        if resp: break
            except Exception as e:
                out["composer_err"] = str(e)[:120]
            out["ogi_count"] = len(resp)
            out["ogi_flag"] = ("UNUSUAL_ACTIVITY" in (resp[0] if resp else ""))
            out["ogi_success"] = bool(resp and "flow-content" in resp[0])
            ctx.close()
    except Exception as e:
        out["fatal"] = str(e)[:200]
    return out

results = []
results.append(run("shell", SHELL, []))
results.append(run("single", FULL, ["--single-process", "--no-zygote"]))
results.append(run("multi", FULL, ["--disable-blink-features=AutomationControlled"]))

os.makedirs("recon", exist_ok=True)
json.dump(results, open("recon/fingerprint_diff.json", "w"), indent=1)

# print a diff table
keys = ["ua", "webdriver", "cores", "devMem", "plugins", "mimeTypes", "hasChrome",
        "hasChromeRuntime", "uaData", "canvas2d", "webgl", "webgl2", "glVendor",
        "glRenderer", "glVersion", "audio", "screen", "dpr", "tz", "grecaptcha"]
print(f"{'signal':<18} | {'shell':<28} | {'single':<28} | {'multi':<28}")
print("-" * 112)
for k in keys:
    row = []
    for r in results:
        v = (r.get("fp") or {}).get(k, "?")
        row.append(str(v)[:28])
    print(f"{k:<18} | {row[0]:<28} | {row[1]:<28} | {row[2]:<28}")
print("\n=== results ===")
for r in results:
    print(f"{r['mode']:<7} mint={r.get('mint')}  ogi_ok={r.get('ogi_success')} "
          f"flagged={r.get('ogi_flag')} count={r.get('ogi_count')} fatal={r.get('fatal','')}")