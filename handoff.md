# Google Flow API Client — Handoff

**Goal (from the user):** a single-file terminal client for Google Flow image generation,
built like `D:\meta-authentication-jailbreak\main.py` — self-contained, curl_cffi,
fast, works "as an API" from the terminal, full feature coverage (models, aspects,
2K/4K upscale, history, downloads, credits). PC load must stay low (one off-screen
Chrome engine, nothing else).

**Hard constraint discovered (do not re-litigate):** Google Flow gates every *write*
endpoint (generation `flowMedia:batchGenerateImages`, upscale `flow:upsampleImage`)
behind reCAPTCHA Enterprise. Tokens are single-use AND bound to the expected action —
a token snatched from a generation request fails on upsample (verified: 403
`PUBLIC_ERROR_UNUSUAL_ACTIVITY`). There is no pure-HTTP generation path. **The browser
engine is the token minter and nothing more.** All reads/downloads are pure HTTP.

---

## Architecture (current)

One file: `flow-reversed\flow_api.py` (~819 lines). Launcher `flow.py` / `flow.bat`
re-execs into the venv. Two classes:

| Class | Role | Pure HTTP? |
|---|---|---|
| `PureHTTP` (line 519) | history, downloads, projects, credits, media detail — curl_cffi `impersonate="chrome146"` + live cookies + bearer | YES |
| `FlowEngine` (line 97) | off-screen Chrome (`profile-copy` profile, port 9333, `--window-position=-32000,-32000`), CDP-driven. Boots, dumps live cookies, drives the composer UI for generation; drives right-click menu for upscale. | no (only where unavoidable) |

`main()` (line 618) = REPL: prompt → generate → auto pure-HTTP download. Commands:
`/model`, `/aspect`, `/credits`, `/history`, `/download`, `/upscale <uuid> [2k|4k]`,
`/projects`, `/project [new|<id>]`, `/options`, `/open`, `/help`, `/quit`.

---

## VERIFIED WORKING (with proof)

1. **Pure-HTTP everything-reads** (`PureHTTP`):
   - `flow.projectInitialData` (history) → 200
   - `media.getMediaUrlRedirect` → 307 → signed CDN `flow-content.google` → 200, byte-identical file (210617B matched)
   - `POST aisandbox /v1/projects:list` (create) + GET projects → 200
   - `POST /v1:getCredits` → 200 `{'credits': 50, 'userPaygateTier': 'PAYGATE_TIER_NOT_PAID'}` (G1_FREEMIUM)
   - Auth = cookies (53 cookies incl. `__Secure-next-auth.session-token`) + `authorization: Bearer` (observed via CDP at boot). **Cookies MUST be freshly dumped by the engine** (`_dump_cookies()` at boot → `sessions\flow.cookies.full.json`); disk-DB decrypt yields corrupted values.
2. **Generation with model/aspect/seed control** — page.route rewrite trick:
   - Register `page.route("**/flowMedia:batchGenerateImages*", ...)` BEFORE navigation (registration while tRPC is polling can hang → Playwright Fetch.enable race), then rewrite the request body while the browser supplies the fresh UI token. Server accepts (200).
   - Verified live: `/aspect portrait` → NARWHAL 768×1376; `/model HARBOR_SEAL` → HARBOR_SEAL 768×1376; `/aspect landscape_4_3` → NARWHAL 1200×896.
3. **2K upscale end-to-end (NEW — the UI path works!)**:
   - Real mouse: right-click the media card → context menu → `Download` → submenu `1K Original size | 2K Upscaled | 4K Upscaled (Upgrade)`.
   - Click `2K Upscaled` → `POST /v1/flow/upsampleImage` → **200 with the new upsampled mediaId in the body** (synchronous; also dedupes — repeated click returns the same media).
   - Downloaded via pure HTTP: `outputs\8646f436-87d9-4bb1-b662-a4739d827a33.jpg` = **2400×1792 (exactly 2× of 1200×896), 683,825 B**. VERIFIED SUCCESS.
   - 4K is paid-tier gated (`Upgrade` label, G1_FREEMIUM can't).
   - **Important nuance:** the upsample request sometimes fires late (the app retries the recaptcha mint — off-screen Chrome + reCAPTCHA connectivity can be slow). Wait up to ~2 min; `page.evaluate` pumping delivers the Playwright events.
4. **Token snatch for pure-HTTP upsample — FAILED (do not retry):** intercepting a generation request to steal its token and replaying it on `upsampleImage` → 403 reCAPTCHA evaluation failed. Token is bound to the generation action. The UI click path above is the only way.

---

## Critical payload structures

**Generation** (exact, from `reports\captured_generation.json`; Content-Type `text/plain;charset=UTF-8`):
```json
{
  "clientContext": {"recaptchaContext": {"token": "..."}, "projectId": "...",
                     "tool": "PINHOLE", "sessionId": "..."},
  "mediaGenerationContext": {"batchId": "..."},
  "useNewMedia": true,
  "requests": [{
    "clientContext": {"recaptchaContext": {"token": "..."}, "projectId": "...",
                       "tool": "PINHOLE", "sessionId": "..."},
    "imageModelName": "NARWHAL",
    "imageAspectRatio": "IMAGE_ASPECT_RATIO_SQUARE",
    "structuredPrompt": {"parts": [{"text": "..."}]},
    "seed": 489772,
    "imageInputs": []
  }]
}
```
**CRITICAL:** `imageModelName` / `imageAspectRatio` / `seed` go INSIDE `requests[0]`.
Top-level `aspectRatio`/`imageAspectRatio` → 400 `Invalid JSON payload received. Unknown name ... Cannot find field`.

**Upscale** (`POST /v1/flow/upsampleImage`):
```json
{"mediaId": "<uuid>", "targetResolution": "UPSAMPLE_IMAGE_RESOLUTION_2K",
 "clientContext": {"recaptchaContext": {"token": "..."}, ...}, "requestContext": {}}
```
- Enums: `UPSAMPLE_IMAGE_RESOLUTION_2K` / `_4K` (underscore required; `...4K` → 400 `Invalid value at 'target_resolution'`).
- `requestContext` schema = `{appletAgentInfo, featureContext, flowCloudTierRequestContext, flowSdkInfo, geminiAgentInfo}` — all optional, send `{}`.
- 200 response body: `{"media": {"name": "<NEW mediaId>", ..., "image": {"generatedImage": {"upsampleMetadata": {"imageUpsampleResolution": "IMAGE_UPSAMPLE_RESOLUTION_2K"}}}}}` — the upsampled item is a **NEW mediaId** (same project).

**Media list item** (`projectInitialData.projectContents.media[]`): `name` = uuid,
`image.generatedImage.{prompt, modelNameType, aspectRatio, seed}`,
`image.dimensions.{width,height}`, `mediaMetadata.mediaBlobSize`, upsampled items carry
`image.generatedImage.upsampleMetadata` (nested — NOT top-level `upsampleMetadata`; old code checked the wrong place).

**Bundle-derived enums** (`reports\bundles\`): models `NARWHAL` `HARBOR_SEAL`
`GEM_PIX_PRO_VERTEX` `R2I`; aspects `IMAGE_ASPECT_RATIO_{SQUARE, PORTRAIT, LANDSCAPE,
PORTRAIT_THREE_FOUR, LANDSCAPE_FOUR_THREE, UNSPECIFIED}` (NO `WIDE`). Upsample models
`GEM_PIX_2_UPSAMPLE_2K/_4K`. `reports\app_config.json`: `isFlowUpsamplingEnabled: true`.

---

## Gotchas / hard-won lessons

- **Kill leftovers by BOTH `9333` AND `profile-copy`** — Chrome renderer processes carry
  only `--user-data-dir` (no port); killing only `*9333*` leaves them holding the profile
  lock → next boot fails "engine Chrome did not start". `_kill_leftovers()` now matches both.
- **`page.route()` must be registered before navigation** (route + polling race → hang).
- **Do NOT pipe python output through `| Out-String` in PowerShell** — it buffers ALL
  output until process exit (looks like a hang). Run `python -u script.py` directly.
- **Playwright CDP**: event attach is `cdp.on("Network.requestWillBeSent", fn)`;
  events only pump while calling Playwright APIs (`page.evaluate` in a loop works).
  `Network.getResponseBody` fails for 204s ("No data found" is normal). Use
  `page.on("response")` + `resp.body()` for bodies — reliable.
- Engine launch args: launch DIRECTLY on `https://labs.google/fx/project/<id>` (no
  `--restore-last-session` — it restored stale tabs and caused goto timeouts); reuse
  `ctx.pages[0]`; `wait_until="commit"`.
- Right-click context menu items have icon+label innerText (`download\nDownload`) —
  match the LAST line. The submenu appears only after clicking `Download` (≈2s).
- Card "More" button (topbar or card `more_vert`) ≠ context menu. Right-click is the
  reliable entry point. Hover first when targeting card-level buttons.
- Off-screen Chrome sometimes shows a toast "Could not connect to the reCAPTCHA
  service…" — usually transient; retry after a few seconds works.
- Media image grid needs ~30–60s to render after open_project (wait on
  `img[src*="getMediaUrlRedirect"]` count > 0).
- Composer selector `[contenteditable='true']`; first Enter often ignored → retry ≤3×.
- `projectInitialData` requires fresh cookies; if `/history` 401s, run one generation
  (or boot the engine) to refresh `sessions\flow.cookies.full.json`.

---

## CURRENT STATE (what to do next)

- `FlowEngine.upscale()` (line 174) was **just rewritten** with the verified UI path
  (right-click → Download → 2K/4K Upscaled → capture 200 mediaId → pure-HTTP download,
  with media_list fallback). **NOT YET TESTED from the REPL** — test first:
  ```
  python -u flow_api.py     # then: /history → pick uuid → /upscale <uuid> 2k
  ```
- Everything else green as of last session (history/download/generation/credits/projects).
- Next candidates after upscale validation:
  1. `/upscale 4k` attempt → expect the paid-gate (`Upgrade` item); make the client report it cleanly.
  2. Test remaining models `GEM_PIX_PRO_VERTEX` / `R2I` via `/model`.
  3. Test aspect `portrait_3_4` / `landscape_4_3` round-trips (portrait & landscape_4_3 already proven).
  4. Polish: verify a full piped (non-interactive) REPL session works for automation use
     (echo prompt | python flow_api.py) so it can be driven as an "API".
  5. Optional: merge the verified upscale path into a pure-HTTP-request snapshot —
     capture the REAL UI upsample request (incl. `clientContext` exact fields) from
     `reports\upsample_request.json` and see if the token+payload can be replayed
     immediately on a SECOND media within the same window (dedupe suggests server-side
     state; likely still 403, low priority).
- Update `README.md` with: verified upscale flow, payload schema corrections, handoff.

---

## File inventory (flow-reversed\)

- `flow_api.py` — THE deliverable (engine + PureHTTP + REPL). Run: `flow.bat` / `python flow.py`
- `flow.py`, `flow.bat` — launchers (venv re-exec)
- `.venv\` — playwright + curl_cffi venv (activation: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned; .\.venv\Scripts\Activate.ps1`)
- `sessions\flow_api_state.json` — project_id / model / aspect / bearer
- `sessions\flow.cookies.full.json` — live cookies for pure-HTTP (refreshed at engine boot)
- `profile-copy\` — engine Chrome profile (NEVER touch the user's real Chrome)
- `outputs\` — generated + upsampled images (`8646f436-...jpg` = proven 2K upscale)
- `reports\bundles\chunk_*.js` (+ fetched lazy chunks) — reverse-engineered app bundle;
  `reports\captured_generation.json` — exact gen payload; `reports\app_config.json`;
  `reports\upsample_request.json` / `upsample_traffic.json` — captured upscale traffic;
  `reports\upsample_responses.json` — 200 response with new mediaId
- Old-backend probes were deleted (2026-09-11 cleanup); captures stay in `reports/`.

## Running it

```
flow-reversed> flow.bat                      # or: python flow.py
you > a red fox in a snowy forest            # generate (square, NARWHAL) + auto-download
you > /model HARBOR_SEAL                     # switch model
you > /aspect landscape_4_3                  # switch aspect
you > /history                               # pure-HTTP list
you > /upscale <uuid> 2k                     # 2K upscale → pure-HTTP download
you > /credits                               # 50 credits, freemium tier
```

---

## 2026-09-11 — migration rebuild (new backend is live, old stack dead)

Google moved Flow to `flow.google.com` + same-origin batchexecute; `flow_api.py`
was rewritten for it and **everything re-verified live** (see README §§1–6).
Old aisandbox/tRPC notes above are history.

- RPC map: `ogiZ0b` gen (sync media + signed CDN), `Zzl0ze` history,
  `nzlxg` credits, `ngNC2` defaults, `SPrCad` upscale (inline base64 JPEG),
  `WuwhI` analytics. Full captures in `reports/` (`rpc_map.txt`,
  `ogiz0b_payload.json`, `sprcad_shape.txt`, `zzl0ze_*.json`, …).
- Gate re-proven: in-page mint + pure-HTTP replay → `PUBLIC_ERROR_UNUSUAL_ACTIVITY`
  (fresh `at` ruled out). Engine types composer; response parse in
  `parse_ogi_response` (note: payload request is at `pay[1][0]`, model idx 5,
  seed idx 3 — the flat-index bug that silently no-op'd rewrites is fixed and
  seed-777-proven).
- Upscale: right-click → Download → 2K → `SPrCad` → base64 → jpg
  (2752×1536 exact 2×). 4K clicks but never responds (tier gate).
- Downloads: gen-time signed URLs (pure HTTP); history re-download via
  engine CDP capture (`download_media`) — lh3/asb URLs 302 to login for curl.
- `flow_server.py` works via new `FlowAPI` facade (`/generate` tested 200).
- Login: user signed in by hand (visible Chrome) after selfie-wizard blocker;
  gotchas in README §5. Session files: `sessions/flow.cookies.full.json`
  (58), `sessions/flow_api_state.json`.
- Next: `/aspect` numeric enum + project list/create rpcids (clean errors now).
- 2026-09-11 cleanup: repo slimmed to `flow.py` (= `python flow.py` → guided
  TUI in `menu.py`), `flow_api.py`, `flow_server.py`, `gen.py`,
  `probe_chromefree_mint.py`, `requirements.txt`, `.env.example`,
  `flow.bat`/`flow.sh`, docs. All old-backend probes, `flow_client.py`,
  stray logs/pids deleted. Resilience pass: retries, self-heal restart,
  env config, Chrome auto-detect, `flow.log`, `/health`+`/restart`.