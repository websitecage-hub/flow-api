"""THE GATE (DIRECTIVE §3) — mint adapter A/B, live, bounded.

Runs AFTER the two sanity gens on today's path. Cells:

  A (control)  browser-mint + HTTP-fire   — isolates the mint variable
  B            HTTP-mint + direct egress + cookies on mint
  C            HTTP-mint + WARP egress  + cookies on mint
  D            HTTP-mint + direct + NO cookies on mint

10 gens per cell, sequential, 2-5 s jittered gaps, <=50 gens total. On a hard
flag (UNUSUAL_ACTIVITY): abort the cell, respect the cooldown, resume later —
never probe into the gate.

Per cell we log: UNUSUAL_ACTIVITY count, soft throttles, media delivered,
end-to-end latency. Decision rule (DIRECTIVE §4) is applied by the human from
the printed table.

USAGE:
  .venv-test/bin/python _gate_run.py --cells A --gens 10
  .venv-test/bin/python _gate_run.py --cells B,C --gens 10
"""
import argparse
import json
import random
import re
import sys
import time
import uuid

sys.path.insert(0, "/home/user/des2/flow-api")
import flow_auth            # noqa: E402
import flow_fire            # noqa: E402
from flow_headless import SITE_KEY, ACTION_IMAGE  # noqa: E402

PROMPTS = [
    "a red fox in snow, cinematic",
    "a lighthouse at night in a storm",
    "a ceramic teapot on a windowsill, morning light",
    "an old map of a fictional island, sepia",
    "a hot air balloon over lavender fields at dawn",
    "a sushi platter on dark slate, top-down",
    "a vintage motorcycle in a garage, dusty light",
    "a field of sunflowers under a huge sky",
    "a snowed-in cabin with warm windows, dusk",
    "a paper boat in a rain puddle, macro",
]


def env(k, d=""):
    for ln in open("/home/user/des2/flow-api/.env", encoding="utf-8"):
        ln = ln.strip()
        if ln.startswith(k + "="):
            return ln.partition("=")[2].strip()
    return d


def fresh_session(use_disk=False):
    jar = flow_auth.mint_cookies(
        env("FLOW_ACCOUNT_EMAIL") or env("FLOW_EMAIL"),
        env("FLOW_MASTER_TOKEN"),
        env("FLOW_ANDROID_ID") or "0123456789abcdef")
    return flow_auth.restore_session(
        records=flow_auth.load_records() if use_disk else None,
        jar=None if use_disk else jar)


def http_mint(session, proxies=None, with_cookies=True):
    """BypassV3 mint with our jar attached (patched bypass_vendor)."""
    import bypass_vendor as bv
    cookies = None
    if with_cookies:
        cookies = [{"name": c.name, "value": c.value,
                    "domain": (c.domain or ".google.com"),
                    "path": c.path or "/",
                    "secure": bool(getattr(c, "secure", True))}
                   for c in session.cookies]
    m = bv.ReCaptchaV3Bypass.from_site_key(
        SITE_KEY, "https://flow.google.com", action=ACTION_IMAGE,
        enterprise=True, cookies=cookies, proxies=proxies)
    return m.bypass()


def browser_mint_token():
    """Cold-spawn the existing engine, mint in-page, tear down."""
    from flow_headless import HeadlessEngine
    eng = HeadlessEngine()
    try:
        eng.start()
        eng.ensure_session(reseed=False)
        return eng.mint(ACTION_IMAGE)
    finally:
        try:
            eng.ctx.close()
        except Exception:
            pass


def fire_generate(session, token, project_id, at=None, bl=None, fsid=None):
    """One pure-HTTP ogiZ0b fire. Returns (ok, flag, media_count, err)."""
    if not (at and bl and fsid):
        r0 = session.get(flow_auth.FLOW_ORIGIN + "/",
                         headers={"User-Agent": flow_auth.UA}, timeout=90)
        h = r0.text
        bl = (re.search(r'"cfb2h":"([^"]+)"', h) or [None, ""])[1]
        fsid = (re.search(r'"FdrFJe":"(-?\d+)"', h) or [None, ""])[1]
        at = (re.search(r'"SNlM0e":"([^"]+)"', h) or [None, ""])[1]
        if not (at and bl and fsid):
            return False, False, 0, "no WIZ data (not logged in?)"
    payload = flow_fire.build_payload(
        prompt=random.choice(PROMPTS), seed=random.randint(1, 10**6),
        aspect="16:9", model_key=36, project_id=project_id, token=token)
    code, data = flow_auth.rpc(session, "ogiZ0b", payload, at, bl, fsid,
                               source_path=f"/project/{project_id}")
    body = json.dumps(data) if data is not None else ""
    if code != 200:
        return False, False, 0, f"HTTP {code}"
    if "UNUSUAL_ACTIVITY" in body:
        return False, True, 0, "UNUSUAL_ACTIVITY"
    if "USER_REQUESTS_THROTTLED" in body:
        return False, False, 0, "SOFT_THROTTLE"
    n = len(re.findall(r"flow-content\.google/image/", body))
    return (n > 0), False, n, ("" if n else "no media in body")


def run_cell(name, mint_mode, proxies=None, with_cookies=True, gens=10):
    print(f"\n=== CELL {name}: mint={mint_mode} "
          f"egress={'WARP' if proxies else 'direct'} "
          f"cookies_on_mint={with_cookies} ===", flush=True)
    session = fresh_session()
    # resolve the reused project id from the engine's state
    pid = (json.load(open("/home/user/des2/flow-api/sessions/state.json"))
           if False else None) or ""
    try:
        st = json.load(open("/home/user/des2/flow-api/sessions/state.json"))
        pid = st.get("project_id", "")
    except Exception:
        pass
    if not pid:
        print("  !! no project id in state — create one on the engine first",
              flush=True)
        return None
    stats = {"flag": 0, "soft": 0, "media": 0, "ok": 0, "lat": [], "err": {}}
    for i in range(gens):
        t0 = time.time()
        try:
            if mint_mode == "browser":
                token = browser_mint_token()
            else:
                token = http_mint(session, proxies=proxies,
                                  with_cookies=with_cookies)
        except Exception as e:
            stats["err"]["mint:" + str(e)[:60]] = \
                stats["err"].get("mint:" + str(e)[:60], 0) + 1
            token = ""
        if not token:
            print(f"  [{i+1}/{gens}] mint FAILED", flush=True)
            stats["err"]["mint-empty"] = stats["err"].get("mint-empty", 0) + 1
            time.sleep(random.uniform(2, 5))
            continue
        ok, flag, n, err = fire_generate(session, token, pid)
        took = time.time() - t0
        if flag:
            stats["flag"] += 1
            print(f"  [{i+1}/{gens}] HARD FLAG after {took:.0f}s — "
                  f"aborting cell, cooldown respected", flush=True)
            break
        if err == "SOFT_THROTTLE":
            stats["soft"] += 1
            time.sleep(random.uniform(30, 60))
            continue
        if ok:
            stats["ok"] += 1
            stats["media"] += n
            stats["lat"].append(took)
        else:
            stats["err"][err[:60]] = stats["err"].get(err[:60], 0) + 1
        print(f"  [{i+1}/{gens}] ok={ok} media={n} {took:.0f}s"
              + (f" err={err[:60]}" if err else ""), flush=True)
        time.sleep(random.uniform(2, 5))
    lat = stats.pop("lat")
    stats["p50_s"] = (sorted(lat)[len(lat)//2] if lat else None)
    print(f"  CELL {name} RESULT: {json.dumps(stats)}", flush=True)
    return stats


def sanity(engine_url, key):
    """2 sanity gens on TODAY's path before any cell (DIRECTIVE §3)."""
    import urllib.request
    H = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    for i in range(2):
        try:
            r = urllib.request.Request(
                engine_url + "/generate",
                data=json.dumps({"prompt": "sanity check " + str(i)}).encode(),
                headers=H, method="POST")
            with urllib.request.urlopen(r, timeout=60) as resp:
                d = json.loads(resp.read())
            print(f"  sanity gen {i+1}: {d.get('job')}", flush=True)
        except Exception as e:
            print(f"  sanity gen {i+1} FAILED: {str(e)[:100]}", flush=True)
            return False
        time.sleep(3)
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", default="A")
    ap.add_argument("--gens", type=int, default=10)
    ap.add_argument("--skip-sanity", action="store_true")
    ap.add_argument("--engine-url", default="http://127.0.0.1:8787")
    ap.add_argument("--key", default="")
    a = ap.parse_args()
    if not a.skip_sanity:
        if not sanity(a.engine_url, a.key):
            sys.exit("sanity gens failed — fix the path before gating")
    warp = {"https": "socks5h://127.0.0.1:1080",
            "http": "socks5h://127.0.0.1:1080"}
    cells = {"A": lambda: run_cell("A", "browser", None, True, a.gens),
             "B": lambda: run_cell("B", "http", None, True, a.gens),
             "C": lambda: run_cell("C", "http", warp, True, a.gens),
             "D": lambda: run_cell("D", "http", None, False, a.gens)}
    for c in a.cells.split(","):
        c = c.strip().upper()
        if c in cells:
            cells[c]()
