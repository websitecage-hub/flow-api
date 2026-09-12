# Google Flow terminal client (account: testflowthrowout@gmail.com)

Live reverse-engineered client for Google Flow image generation + 2K upscale.
Terminal REPL (`flow.bat` / `flow.py`) and REST server (`flow_server.py`).

> **2026-09-11 migration:** Google moved Flow from `labs.google/fx` (now 308s
> to `flow.google.com`) to a same-origin **batchexecute** backend. The old
> `aisandbox-pa` + tRPC paths documented in git history are dead. Everything
> below was re-verified live on the new backend (20+ images generated,
> 2K upscale at exactly 2x, pure-HTTP reads).

All credentials live under `sessions/` (git-ignored). Do not commit them.

---

## 1. Backend (verified live)

Base: `https://flow.google.com/u/1/_/AiSandboxAngularFrontend/data/batchexecute`
`POST ?rpcids=<id>&source-path=/u/1/project/<id>` with
`f.req=[[["<id>","<json payload>",null,"generic"]]]&at=<page token>`
(content-type `application/x-www-form-urlencoded`). Auth = `flow.google.com`
cookies + `at` token scraped from project-page HTML (rotates; re-scrape per
run — `PureHTTP.at()`).

| rpcid | Payload | Response |
|-------|---------|----------|
| `ogiZ0b` | `[null,[[null,null,null,seed,ASPECT,MODEL,null,[null,22,null,null,null,projectId,…,[recaptcha,1]],[[[[prompt]]]],null,null,null,batchA,batchB]],1,…]` — ASPECT enum: `1:1→1, 9:16→2, 16:9→3, 3:4→4, 4:3→5`; MODEL: `NARWHAL` (Nano Banana 2), `HARBOR_SEAL` (2 Lite), `GEM_PIX_2` (Pro); count = repeated submits (UI xN fires N POSTs, no count field) | **sync 200** (details as before) |
| `Zzl0ze` | `["projects/<id>", null, null, null, [1]]` | project history: el1 = batches `[batch, …, [title, createTime, …, media, workflow, updateTime], project]`; el2 = media `[uuid, project, batch, "CAE", null, [createTime, …, thumb, promptNest, …, url, …, size, genBlock]]` |
| `nzlxg` | `[]` | credits `[50,3,8,1,null,50]` (50 free on this tier; generation + 2K cost 0) |
| `ngNC2` | `["tools/PINHOLE/projects/<id>"]` | generation defaults (`narwhal_display`, `veo_3_1_lite`) |
| `SPrCad` | `["<media_uuid>", 1, [null,22,null×8,[recaptcha,1]]]` | **sync 200**: `[meta, base64_jpeg]` — upscaled bytes inline |
| `yBhWQ` / `HTrJv` | video-model availability / app+model config |
| `WuwhI` | analytics (ignore) |

Reads (`Zzl0ze`, `nzlxg`, `ngNC2`) are pure HTTP (curl_cffi `chrome146`).
Writes (`ogiZ0b`, `SPrCad`) require a UI-minted reCAPTCHA token.

## 2. The reCAPTCHA gate (still holds on the new backend)

- In-page `grecaptcha.enterprise.execute(..., {action:'GENERATE'})` mint +
  pure-HTTP `ogiZ0b` replay → 200 transport, app error
  `PUBLIC_ERROR_UNUSUAL_ACTIVITY` (fresh `at`, fresh token — `at` ruled out,
  it's identical in live page HTML).
- Real composer typing + Enter → 200 with media (every time).
- **Conclusion stands: generation/upscale only via the driven UI.** The
  engine is the token minter and nothing more. (`probe_chromefree_mint.py`:
  `enterprise.js` is a loader stub; `/reload` needs BotGuard POST — no
  browser-free mint. Chrome-free generation is out of scope.)

## 3. Downloads

- Generation responses carry **signed `flow-content.google/image/<uuid>?Expires=…`
  URLs** — plain GET, no auth, byte-identical to server `mediaBlobSize`.
- History `lh3…/asb/` URLs 302 to a login wall for plain HTTP (auth-bound).
  The app serves them same-origin at `flow.google.com/asb/<token>` (renders
  1376×768 in-page). Re-download therefore goes through the engine: CDP
  `response.body()` capture on project reload (`FlowEngine.download_media`).

## 4. Running it

```
flow-reversed> flow.bat                      # guided menu (double-click friendly)
  ==== Google Flow ====
    1. Generate image    <- type prompt, pick model/seed, optional 2K
    2. Upscale to 2K     <- pick from numbered history, no UUID typing
    3. History  4. Download  5. Credits  6. Settings  0. Quit
flow-reversed> python gen.py "a red fox"     # one-shot: generate + download
python gen.py "prompt" -m PRO -a 3:4 -n 2 -s 777 --upscale 2K --json
  # -m NARWHAL|HARBOR_SEAL|GEM_PIX_2 (Nano Banana 2|Lite|Pro)
  # -a 16:9|4:3|1:1|3:4|9:16   -n 1-4 (count)   --reuse 3 (by history #)
python gen.py --history | --credits | --status        # pure HTTP, no browser
python gen.py --download <uuid> | --upscale-only <uuid>
python flow_server.py --port 8787           # REST (see §4b)
```

`gen.py` exit codes: 0 ok · 1 usage/config · 2 failed. `--json` for scripting.

REPL commands (`flow.bat`): `<prompt>` `/model` `/seed` `/history`
`/download` `/upscale [2k|4k]` `/credits` `/options` `/open` `/project`
`/help` `/quit`.

### 4b. REST server (async jobs + forever-logs)

```
  POST /generate {"prompt":"...","count":1} -> 202 {"job","poll":"/jobs/<id>"}
  POST /generate?wait=1 ...                     -> 200 full result (blocking)
  POST /upscale  {"mediaId":"<uuid>"}           -> 202 job (same pattern)
  GET  /jobs /jobs/<id>                         -> queue + results (sqlite)
  GET  /history (live, else last-known mirror flagged stale) /credits
  GET  /health (engine + session) /restart /session (login steps when dead)
  GET  /logs?limit=100                          -> daily append-only JSONL
  GET  /media/<uuid>                            -> image bytes
```
Jobs persist in `sessions/flow.db`; logs in `logs/flow-YYYYMMDD.jsonl`
(kept indefinitely). Slow ops never block the server: poll the job.

`flow_api.py` is the core: `PureHTTP` (reads/downloads) + `FlowEngine`
(off-screen Chrome, port 9333) + `FlowAPI` (worker-thread facade) + REPL.
`flow.py`/`flow.bat`/`flow.sh` bootstrap the venv automatically.

### 4c. Run on any PC

1. Install Chrome + Python 3.10+.
2. Copy this folder (or git clone). `sessions/` and `profile-copy/` are
   git-ignored — bring your own (or sign in once, see §5).
3. Run `flow.bat` (Windows) / `./flow.sh` (mac/Linux) — first run creates
   `.venv` and installs `requirements.txt` by itself.
4. Config without editing code: copy `.env.example` → `.env`
   (`FLOW_PROFILE`, `FLOW_CHROME`, `FLOW_PORT`, `FLOW_PROJECT`, …).
   Every path/timeout is env-overridable; Chrome auto-detected on
   Windows/macOS/Linux (off-screen headless outside Windows).

### 4d. Self-healing behavior

- All HTTP calls retry with backoff; stale `at` tokens refresh + retry.
- Pre-op guardian on every engine op: dead CDP → restart; dead session →
  cookie refresh → re-login attempt → clean error with manual steps
  (also served at `GET /session`).
- Empty results self-heal too: composer-pollution (stale chips swallowing
  Enter) triggers restart-with-fresh-page + one retry — proven live after a
  wedged tab failed a job and the retry returned 200.
- Every run logs to `flow.log` + daily `logs/*.jsonl` (forever); `/health`
  reports `engine_alive` + `session_ok`; `/restart` heals remotely.
- Clear failure messages for: no Chrome, missing profile, dead session
  (sign in once visibly), locked profile, 4K tier gate.

## 5. Login notes (post-migration gotchas)

- App URLs carry the multi-login prefix: `/u/1/project/<id>`.
- `labs.google/fx/...` 308-redirects; logged-out deep links land on
  `/about` marketing — login starts at the `Create with Google Flow` CTA.
- Google changed the identifier field: `input[type=email]` no longer exists,
  use `#identifierId` (`input[type=text]`). Password: visible
  `input[type=password]` (ignore hidden decoys). Dismiss passkey with
  `Not now`, gds-wizard cards with `Skip/Cancel`.
- The profile holds 2 Google accounts; OAuth may default to the wrong one
  (`confirmidentifier`) — force the identifier page with `&Email=…`.
- Off-screen Chrome needs `--disable-background-{timer-throttling,backgrounding-occluded-windows,renderer-backgrounding}`
  or the app never fires requests; pump CDP events with `page.evaluate("1")`;
  media grid needs ~30–60s to render; first Enter is sometimes ignored
  (re-press only while nothing observed — no request means no charge).
- Session check = pure-HTTP `Zzl0ze` 200 (not labs tRPC — dead).

## 6. Status (2026-09-11, throwaway account)
- ✅ Off-screen boot + session (pure-HTTP check, auto-login fallback kept)
- ✅ `/credits` 50 · `/history` full detail · `/options` defaults
- ✅ Generate NARWHAL (+ signed-URL auto-download), 20+ images in `outputs/`
- ✅ `/seed` fixed-seed proven (777 in history) → `/model` rewrite same path
- ✅ `/download` via engine CDP capture
- ✅ `/upscale 2k` end-to-end (`3a2ba23f…jpg` 2752×1536 = exactly 2×)
- ✅ Image controls mapped from UI screenshots: 3 models (Pro=`GEM_PIX_2`
  validated live → 896×1200; 2 Lite=`HARBOR_SEAL`), all 5 aspects
  (wire enum via diff captures; 3:4 validated live), count x1–x4
  (= N submits, proven: 1 POST with x1), `--reuse N`
- ❌ `/upscale 4k`: menu exists, click accepted, no `SPrCad` within 180s (tier gate)
- ⏳ Video tab mapped read-only (Frames/Ingredients, 16:9/9:16,
  Omni 1.1 Flash / Veo 3.1 Lite/Fast/Quality, 20 credits, 720p·8s) —
  submit NOT wired (costs real credits; say go to spend 20 on one probe)
- ⏳ Project list/create rpcids unmapped (`/projects`, `/project new` errors)

## 7. Watermark (investigated 2026-09-11 — nothing to remove)

- Pixels are clean: no visible watermark, logo, or banner on outputs
  (checked config traffic, full UI text dump, and image forensics).
- What exists is provenance metadata, verified in a real output file:
  IPTC/XMP `Credit="Made with Google AI"`, `DigitalSourceType=
  trainedAlgorithmicMedia`, plus a Google-signed **C2PA manifest** stating
  `c2pa.created` ("Created by Google Generative AI") and `c2pa.edited`
  (**"Applied imperceptible SynthID watermark"**).
- There is no UI toggle, no config key, and no request field controlling it
  (zero hits for watermark/synthid/logo in new-backend traffic and UI).
  Stripping signed provenance would break the C2PA signature (detectable) —
  deliberately not built. If a *visible* mark ever appears on an image, it
  is model-rendered scene content (prompt artifact): regenerate with a new
  seed instead.

## 8. Speed (what makes it fast)

- `gen.py --keep` leaves the engine running; the next command reuses it
  (warm submit in ~15s vs ~60s+ cold boot).
- Early result collection (stops once 2+ unique media parsed, min 10s),
  parallel twin downloads, adaptive page settle (no fixed sleeps when live).
- Pure-HTTP paths (`--history/--credits/--status`) never boot the browser.
- The floor is the UI roundtrip itself (~30–60s/gen) — the reCAPTCHA gate
  (see §2) forbids anything faster.

## 9. Files (slim layout)

```
flow.py            one command: setup + guided TUI  (python flow.py)
menu.py            the TUI (prompts + numbered features)
gen.py             one-shot CLI (prompt/flags/--json, exit codes)
flow_api.py        core library: PureHTTP + FlowEngine + FlowAPI
flow_server.py     REST (/generate /upscale /history /credits /health /restart)
probe_chromefree_mint.py  chrome-free mint probe (evidence for §2)
requirements.txt / .env.example / flow.bat / flow.sh
reports/           captures + rpc map (evidence)   sessions/  git-ignored auth
profile-copy/      git-ignored Chrome profile      outputs/   images
flow.log           run log (auto-written)
```
