# CONTEXT — Google Flow API (read this first in a new session)

Last updated: 2026-10-07. Everything below was **tested live** against
`flow.google.com` with a real signed-in account. "Verified" = there is a
captured response in this session's logs / the repo.

---

## 0. The goal

Reverse-engineer Google Flow (image generation) into an **HTTP API** usable by
a content-automation system, ideally running **self-contained on Render free
tier (512 MB RAM)**. Owner wants it always-live and low-maintenance.

## 1. Bottom line (read this, it saves hours)

- **Reads work on free tier.** credits / history / app-config / options run in
  `chrome-headless-shell` at **~115–120 MB RSS** — comfortably inside 512 MB.
- **Generation is BLOCKED by reCAPTCHA Enterprise fingerprinting**, not by our
  code and not by RAM. Any headless/automated browser gets
  `PUBLIC_ERROR_UNUSUAL_ACTIVITY` on `ogiZ0b`.
- Proof it's the fingerprint, not the payload: driving **the app's own
  composer** (real click+type+Enter) in `chrome-headless-shell` → fails the
  same way. In full **multi-process** Chromium the composer **succeeds** and
  returns a real image (`1cb7192d-…`), but a programmatic `fetch()` of the
  byte-identical request in that same session is **still** flagged. → Google
  scores the browser/flow, not just the bytes.
- Full Chromium needs **~1.4 GB** → cannot run on free 512 MB. So
  "generation on free tier, HTTP-only" is **not achievable**; Google's bot
  defence blocks it.

## 2. The two decisions still open

Generation must run somewhere with a real browser. Options:
1. **Worker-on-PC** (split-brain): free Render = queue + reads + API; owner's
   PC runs full Chrome (`worker.py`) for generation. Reliable now.
2. **Fingerprint work**: camoufox / undetected-chromedriver style non-headless
   reproducing the in-app XHR. Unproven; reCAPTCHA Enterprise is hard.
3. **Reads-only API on free tier** + generation elsewhere.

Not yet implemented: **self-healing** (auto re-mint tokens, restart engine on
death, reload cookies). Requested by owner, not built.

## 3. Verified wire facts (do NOT re-guess these)

- Backend: same-origin batchexecute
  `POST /u/<N>/_/AiSandboxAngularFrontend/data/batchexecute?rpcids=<id>&source-path=<path>`
  body `f.req=[[["<rpcid>","<json payload>",null,"generic"]]]&at=<xsrf>`.
- **`at` token = `WIZ_global_data.SNlM0e`** (shape `[A-Za-z0-9_-]+:\d{13}`).
  The `AIQ-…` pattern the old code hunted is a **decoy**.
- **reCAPTCHA Enterprise action for images = `IMAGE_GENERATION`**
  (the app calls `Uo("IMAGE_GENERATION")`; `"GENERATE"` is wrong).
- Site key = `WIZ_global_data.xZbWve` =
  `6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV`.
- **`ogiZ0b` = `/FlowService.BatchGenerateImages`**, payload:
  ```
  [null, [request...], 1, clientContext, [batchId]]
  clientContext = [null,22,null,null,null,<projectId>,null,null,null,null,[<token>,1]]
  request = [null,null,null,<seed>,<aspectInt>,<imageModelKey>,null,clientContext,
             [[[<prompt>]]],null,null,null,<UUID_UPPER>,<UUID_UPPER>]
  ```
  Field is **`imageModelKey`** (not `imageModelName`); token at
  `clientContext[10]` (not `[9]`). Captured real request:
  `recon/auth/ogiz0b_real.json` (git-ignored; regenerate with the method in §6).
- Aspect ints: `1:1→1, 9:16→2, 16:9→3, 3:4→4, 4:3→5`.
- Other RPCs: `nzlxg` GetCredits · `cPZSdc` GetFlowAppConfig · `Zzl0ze` history
  · `okjlg` ListProjects · `SPrCad` upscale (meta + inline base64 JPEG).
- Status codes: **401** = not logged in · **400 + `"xsrf"`** = bad `at` ·
  **200 + `google.rpc.ErrorInfo`** = reached backend (read the error).

## 4. Models (verified from the app's own bundle enum)

Image-model enum (id → code): NARWHAL=29, HARBOR_SEAL=31, GEM_PIX_2=25,
GEM_PIX=23, GEM_PIX_2_BOTTLE=28, GEM_PIX_2_GEOGENIE=30, R2I=24,
+ vertex variants, and **BELUGA=36 / BELUGA_THINKING_LOW=37 / _MED=38 /
_HIGH=39 / BELUGA_VERTEX=40**.

- **`BELUGA` = Nano Banana 2.1** (GA 2026-10-06, `gemini-nano-banana-2.1`).
  This is the **default** now (`DEFAULT_MODEL = "BELUGA"`).
- `NARWHAL` = Nano Banana 2 → **shut down 2026-10-29**.
- `HARBOR_SEAL` = Nano Banana 2 Lite · `GEM_PIX_2` = Nano Banana Pro.
- App build string seen: `2026-10-06-v0-nb21-…` (nb21 confirms 2.1 live).

## 5. Memory (the free-tier story)

| engine | RSS loaded | passes reCAPTCHA? |
|---|---|---|
| full Chrome `--headless=new` (old `flow_api.py`) | **1213 MB** | yes (composer) |
| full Chrome normal multi-proc | ~1400 MB | yes (composer) |
| full Chrome `--single-process` | ~531 MB | **no** |
| `chrome-headless-shell` (new `flow_headless.py`) | **115–120 MB** | **no** |

The old repo's "free tier OOMs, need paid + home worker" came from the
1213 MB number — it measured the *heavy* headless mode. The light one fits,
but is fingerprint-blocked for writes.

## 6. How to reproduce / verify (the method that works)

1. Put Flow cookies in `sessions/flow.cookies.full.json` (git-ignored) or set
   `FLOW_COOKIES_JSON` (JSON array, or base64 of it — decoded at boot).
   Needed `.google.com` cookies: `SID/HSID/SSID/APISID/SAPISID`,
   `__Secure-1PSID`, `__Secure-3PSID` (`__Secure-*` must be `secure:true`).
2. Engine injects them, opens `/u/<N>/project/<pid>`.
3. Scrape `WIZ_global_data` (`SNlM0e`, `xZbWve`, `FdrFJe`).
4. To get the real payload: drive the composer once and intercept the request:
   `page.on("request")` → `r.post_data` for `rpcids=ogiZ0b`.
   Read the response: success has `flow-content.google`, failure has
   `UNUSUAL_ACTIVITY`.
5. Auth-gated bundle chunks (grep for the enum / `Uo(` / `BatchGenerateImages`):
   URLs contain `boq-labs-ai-sandbox` and `.../ck=...`; `flow_headless.py`
   `capture_chunks()` lists them.

Recon scripts: `recon_fp.py` (fingerprint diff across the 3 modes),
`recon_batchexecute.py`, `recon_recaptcha.py`, `recon_capture.py`.

## 7. Files

**New this session (the working headless path):**
- `flow_headless.py` — `chrome-headless-shell` engine: cookie inject, mint,
  in-page batchexecute, credits/history/config, generate/upscale (verified
  schema), `capture_ui()` / `capture_chunks()`.
- `flow_api_headless.py` — `FlowAPI`-compatible facade so `flow_server.py`
  runs unchanged with `FLOW_ENGINE=headless`.
- `import_cookies.py` — DevTools/Cookie-Editor export → `sessions/…json` +
  base64 blob for `FLOW_COOKIES_JSON`.
- `Dockerfile.headless`, `render.yaml` (plan: free), `start.sh` (picks the
  light binary), `DEPLOY-FREE.md` (deploy + phone-first login), `VERDICT.md`.
- `.gitignore` — added `profile-*` (test profiles hold live cookies), `recon/`.

**Existing (unchanged unless noted):** `flow_api.py` (old full-Chrome engine),
`flow_server.py` (engine selected by `FLOW_ENGINE`), `flow_store.py` (sqlite
jobs/media/logs), `worker.py` (home-PC worker), `gen.py`, `menu.py`, `flow.py`.

## 8. Run it

```
# free-tier reads (needs cookies)
FLOW_ENGINE=headless FLOW_COOKIES_JSON="$(cat sessions/flow.cookies.full.json)" \
FLOW_PROJECT=<uuid> python flow_server.py --port 8787
#   GET /health  /credits  /history  ; POST /generate -> job (will hit the wall)

# capture UI options + model list from the live app (needs login)
python flow_headless.py --capture-ui
python flow_headless.py --capture-chunks
```

Env: `FLOW_ENGINE=headless|chrome` · `FLOW_COOKIES_JSON` · `FLOW_PROJECT` ·
`FLOW_PROFILE` · `FLOW_OUT` · `API_KEYS` (`cms1:secret`) · `RATE_*` ·
`FLOW_MEM_GUARD_MB` (default 420) · `PROFILE_TAR_URL` (profile tarball seed).

## 9. Deploy (Render free)

Blueprint `render.yaml` → `Dockerfile.headless`, `plan: free`,
`FLOW_ENGINE=headless`. Set `FLOW_PROJECT` + `API_KEYS`. Free tier has **no
persistent disk**, so seed the login via `FLOW_COOKIES_JSON` (phone-friendly)
or `PROFILE_TAR_URL`. Free instances **sleep after 15 min idle** (cold start
~20–40 s). Reads work; generation will 5xx/flag until §2 is decided.

## 10. Security (do these)

- **Rotate** the API key `cms1:c23737…` that was committed in
  `render_create.py`/`render_envset.py`/`render_recreate.py` (repo is public;
  still in git history).
- **Revoke** the GitHub PAT used to push (it appeared in chat).
- Flow cookies = live Google session. Never commit them. `sessions/` and
  `profile-*` are git-ignored; **verify `git ls-files` before pushing**.

## 11. Session log (what actually happened)

- Cloned the repo; read every file; found the central flaw = the memory
  measurement used full headless Chrome (1213 MB), so it wrongly concluded
  free tier is impossible + split-brain was required.
- Discovered `chrome-headless-shell` runs the app at ~115 MB → free tier fits.
- Built the headless engine; verified transport reaches Google (401 logged out).
- Owner supplied cookies (Kiwi browser export) → reads work (50 credits, 8
  history items). Fixed `at` token (`SNlM0e`) and cookie `sameSite`.
- Extracted the model enum + master list from the auth-gated bundle →
  found **BELUGA = Nano Banana 2.1**.
- Captured the real `ogiZ0b` request by intercepting the composer → fixed the
  payload (positional, `imageModelKey`, token index 10) and the action
  (`IMAGE_GENERATION`).
- Proved the wall: headless fails (even the app's own composer);
  multi-process full Chrome composer succeeds; direct fetch still flagged;
  `--single-process` fails at ~531 MB; full ~1.4 GB.
- Pushed 4 commits to `websitecage-hub/flow-api` (main @ `c756337`).
- Saved skill `google-flow-reverse-engineering`.

## 12. Do next (pick up here)

1. Decide §2 (worker-on-PC vs fingerprint vs reads-only).
2. Build §11 self-healing (token re-mint, engine restart, cookie reload).
3. Run `recon_fp.py` to name the exact fingerprint signal (open question:
   *why* multi-process passes and single-process/shell fail).
4. If worker path: re-test `worker.py` end-to-end with the new schema.
5. If fingerprint path: try camoufox reproducing the in-app XHR.