# FLOW-INTERNALS.md — complete technical reference

Everything in this document was **reverse-engineered and verified live** against
`flow.google.com` with a real signed-in account on **2026-10-10**, and extended
on **2026-10-11 from a full HAR capture** (`flow.google.com(testingflow).har`,
224 entries — includes a complete sign-in + generation session). Where something
is `VERIFIED`, there is a captured request/response behind it. Where something
is `UNVERIFIED`, it says so.

This is the deep reference. For quickstart see `README.md`; for deploy see
`DEPLOY-FREE.md`; for the earlier handoff narrative see `CONTEXT.md`.

> **Secrets warning.** This system authenticates with a live Google session
> (cookies). Those are secrets. They live only in `sessions/` (git-ignored) or in
> the `FLOW_COOKIES_JSON` env var. Never commit them. Rotate after testing. Use a
> **throwaway account** — never a main Google account.

---

## 0. TL;DR

- Google Flow's web app talks to **one same-origin endpoint**: `batchexecute`
  at `https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute`.
- Reads (credits, project list, history, config) are plain authenticated POSTs.
- **Writes** (image generation, upscale) are gated by **reCAPTCHA Enterprise**
  and can only be performed by the app's own UI flow — so generation is done by
  **driving the real composer** in a hidden browser, not by replaying a fetch.
- The whole engine runs in **~130 MB RSS** using `chrome-headless-shell`, which
  fits Render's free 512 MB tier. (Full Chromium needs ~1.6 GB.)
- Everything is exposed as a self-healing HTTP API (`flow_server.py`).

---

## 1. System architecture

```
 content systems / your scripts
        │  HTTP + Bearer key
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ flow_server.py        ThreadingHTTPServer, JSON, CORS, auth,  │
 │                       rate limits, /diag, /jobs, /cookies     │
 └───────────────┬──────────────────────────────────────────────┘
                 │ calls (serialized)
                 ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ flow_api_headless.FlowAPI   single worker thread + supervisor │
 │   • one Playwright op at a time (sync API is thread-bound)    │
 │   • liveness probe, hard rebuild, session self-heal           │
 │   • sqlite job queue (flow_store)                             │
 └───────────────┬──────────────────────────────────────────────┘
                 │ runs inside the page
                 ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ flow_headless.HeadlessEngine                                  │
 │   chrome-headless-shell (1 process, ~130 MB)                  │
 │   • stealth + client-hint spoofing                            │
 │   • recaptcha mint (grecaptcha.enterprise.execute)            │
 │   • batchexecute RPCs (copy-the-headers)                      │
 │   • composer drive for generation                             │
 │   • signed-URL download                                       │
 └──────────────────────────────────────────────────────────────┘
```

Two engines exist:
- **`flow_headless.py`** — the *light* engine (the one used everywhere now).
- **`flow_api.py`** — the old *full-Chromium* engine (~1.2–1.6 GB). Kept only for
  compatibility and local dev; never selected when `FLOW_ENGINE=headless`.

---

## 2. The wire protocol (VERIFIED)

### 2.1 Base endpoint

The app's real calls go to:

```
POST https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute
     ?rpcids=<rpcid>
     &source-path=%2F                     (URL-encoded path; "/" on home)
     &bl=boq_labs-ai-sandbox-frontend_20261007.14_p0
     &f.sid=<session id>
     &hl=en-GB
     &_reqid=<monotonic>
     &rt=c
Content-Type: application/x-www-form-urlencoded;charset=UTF-8
X-Same-Domain: 1
```

**⚠️ There is NO `/u/1/` prefix.** This was the single biggest bug in the old
code: it hardcoded `/u/1/_/AiSandboxAngularFrontend/...` and `/u/1/project/<id>`.
Both are stale; they produce **HTTP 400** on every RPC and **404** on project
pages (`/u/1/404?reason=project`). The mult-login prefix is gone.

### 2.2 Request body

```
f.req=[[["<rpcid>","<json-stringified-payload>",null,"generic"]]]
&at=<xsrf-token>
```

- `f.req` is `URLSearchParams`-encoded JSON (the payload is itself a JSON string).
- `at` is the **XSRF/batchexecute token**, shape `[A-Za-z0-9_-]+:<13-digit-ms>`.
  It **must include the `:timestamp`** or Google returns 400.
  Source: `WIZ_global_data.SNlM0e`, and it rotates per page.

### 2.3 Response framing

XSSI-guarded, newline-delimited:

```
)]}'

<byte-length>
[["wrb.fr","<rpcid>","<json-stringified-result>",null,null,null,"generic"],["di",..],["af.httprm",..]]
<byte-length>
[["e",4,null,null,<len>]]
```

Parse: skip the `)]}'` line, then for each line starting with `[["wrb.fr"`,
`json.loads` it and take `chunk[2]` (itself JSON) for the payload.

### 2.4 Status codes

| Status | Meaning |
|---|---|
| 200 + `wrb.fr` data | success |
| **401** | not logged in / session dead |
| **400** | malformed request (wrong path, missing `x-same-domain`, bad `at`) |
| 200 + `google.rpc.ErrorInfo` in body | reached backend; read the app error |

### 2.5 Copy-the-headers (why it survives changes)

Rather than hardcoding the URL, the engine **listens to the app's own outgoing
requests** and captures:
- the first `batchexecute` URL (path + `bl`, `f.sid`, `hl`, `rt` params), and
- the `at` token from that request's POST body.

Our RPCs then reuse that captured URL (swapping only `rpcids` and
`source-path`). If capture fails, it falls back to the known 2026-10-10 shape.
This is why endpoint drift no longer breaks reads. (See `JS_RPC` and the
`_grab` request listener in `flow_headless.py`.)

---

## 3. RPC map (all rpcids)

| rpcid | Meaning | Payload (JSON) | Response (shape) | Status |
|---|---|---|---|---|
| `o30O0e` | whoami / "me" | `[["me"],[[["person.photo","person.name","person.email","person.metadata"]],null,[1,7,10]]]` | profile fields | VERIFIED |
| `nzlxg` | **GetCredits** | `[]` | `[50,3,8,1,null,50,[<ts>]]` → `[0]` = credits | VERIFIED |
| `cPZSdc` | GetFlowAppConfig | `[]` | build string + banners | VERIFIED |
| `Yizz8d` | home defaults | `[]` | `["<model>","<build>",["<mediaId>"],2,...,"grid",...,["AGENT","MODEL"],...]` | VERIFIED |
| `UpteDb` | **ListProjects** | `["projects/*",21,null,null,null,null,[1]]` | `[[[<projectUuid>,[<name>,null,[<ts>],"<thumbUrl>","<mediaUuid>"]], ...]]` | VERIFIED |
| `jHPbke` | **CreateProject** | `[]` | `["<newProjectUuid>",[<name>]]` e.g. `["c705d3ef-...",["Oct 10 - 20:21"]]` | VERIFIED |
| `Zzl0ze` | project history (batches + media) | `["projects/<id>",null,null,null,[1]]` | `[batches, media]` | VERIFIED (earlier) |
| `ogiZ0b` | **BatchGenerateImages** | see §5 | sync media + signed CDN URLs | VERIFIED |
| `SPrCad` | upscale (2K/4K) | `["<media_uuid>",1,[null,22,null×8,[<token>,1]]]` | `[meta, base64JPEG]` | VERIFIED (earlier) |
| `ngNC2` | generation defaults | `["tools/PINHOLE/projects/<id>"]` | defaults | VERIFIED (earlier) |
| `HTrJv` | app+model config | — | config | seen |
| `yBhWQ` | video-model availability | — | config | seen |
| `KV2T2d` | small config | `[22]` | — | seen |
| `NfrxTb` | small config | `[16]` | — | seen |
| `xI9TVb` | preferences | `["userPreferences/"]` | — | seen |
| `as29s` | media detail | `["<media_uuid>"]` | media + signed URLs | VERIFIED |
| `LPzVkd`,`qJcgMc`,`mrlkwd`,`tRARke`,`ve2Lsc` | misc app data (unmapped names) | — | — | seen |
| `WuwhI` | analytics (ignore) | — | — | seen |

> `UNVERIFIED` names (`LPzVkd` etc.) appear in real traffic but their exact
> semantics aren't mapped yet — a full HAR would resolve them.

### 3.1 ListProjects — exact response (VERIFIED)

```json
[[["aa3bb64b-d58c-4d28-8fef-9120a6b131e3",
   ["Sept 14 - 23:06", null, [1789407408, 84519000],
    "https://lh3.googleusercontent.com/asb/ANqv...", "c8f10025-92cc-46ee-8b79-adb414b09b28"]],
  ["a4669748-1221-4a6b-b4ac-fc5e6752e3aa", ["Aug 21, 09:40 pm", ...]]]]
```

`rec[0]` = project uuid; `rec[1][0]` = display name; `rec[1][3]` = thumbnail;
`rec[1][4]` = media uuid used as the thumbnail.

### 3.2 CreateProject — exact (VERIFIED, HAR)

App's real payload (HAR 2026-10-11):

```json
["projects/*", [null, ["Oct 11 - 19:01"]], [null, 22]]
→ ["a39ec888-fe80-4440-aa05-9a2918324f8d", ["Oct 11 - 19:01"]]
```

A bare `[]` also works (verified live 2026-10-10 → `["a80f9be1-…", []]`).
The engine sends the **named** shape (project gets a timestamp name) and falls
back to `[]`. Browser entry point: the gallery **"New project"** button.

### 3.3 The other RPCs (payloads from the HAR)

| rpcid | Payload | Notes |
|---|---|---|
| `UEkKwb` | `["<dsh>"]` | sign-in session init (accounts.google.com) |
| `MI613e` | `[null,"<email>",1,…]` | **sign-in: email step** |
| `B4hajb` | `[1,1,null,[1,…,[name]],[…,"https://flow.google.com/"],…,identity-signin-password,…]` | **sign-in: password step** |
| `DTaVef` | `[1]` | heartbeat/feature flag |
| `LPzVkd` | `[[4,8,5,6,9]]` | feature/flag query |
| `mrlkwd` | `["<projectId>"]` | project meta poll |
| `qJcgMc` | `[]` | — |
| `ve2Lsc` | `[]` | — |
| `HTrJv` | `[]` | app+model config (33 KB response) |
| `tRARke` | `[]` | config (47 KB response) |
| `as29s` | `["<mediaUuid>"]` | media detail |

### 3.4 ⭐ The sign-in flow (HAR-verified — the auto-login blueprint)

This throwaway account is **password-only (no 2FA)**. Full verified sequence on
`accounts.google.com/v3/signin/_/AccountsSignInUi/data/batchexecute`
(`bl=boq_identityfrontendauthuiserver_20261006.03_p0`):

```
UEkKwb  ["<dsh>"]                              session init (dsh = S-…:…)
MI613e  [null,"<email>",1,…]                   identifier step      200
B4hajb  [1,1,null,[1,…],…,"identity-signin-password","!…"]   password step    200
→ next page load lands on flow.google.com with a working session
```

The password is **client-side-encrypted in transit** (`!…` blob) — it is NOT
recoverable from a HAR (good). Therefore auto-login is implemented by **driving
the real sign-in UI** (`engine.login()`): `#identifierId` → email → Next →
`input[type=password]` → password → Next → wait for `flow.google.com`.
Chained into `ensure_session()`: cookies re-seed → if still dead and
`FLOW_EMAIL`+`FLOW_PASSWORD` are set → full sign-in.

---

## 4. Models & encodings

### 4.1 Image-model enum (from the app's own bundle)

```
20 IMAGEN_3_5_FAST   21 IMAGEN_3_5        23 GEM_PIX
24 R2I               25 GEM_PIX_2         26 GEM_PIX_2_UPSAMPLE_2K
27 GEM_PIX_2_UPSAMPLE_4K                  28 GEM_PIX_2_BOTTLE
29 NARWHAL           30 GEM_PIX_2_GEOGENIE
31 HARBOR_SEAL       32 GEM_PIX_VERTEX    33 GEM_PIX_2_VERTEX
34 GEM_PIX_PRO_VERTEX                     35 GEM_PIX_2_LITE_VERTEX
36 BELUGA            37 BELUGA_THINKING_LOW
38 BELUGA_THINKING_MED                    39 BELUGA_THINKING_HIGH
40 BELUGA_VERTEX
```

| Wire code | Display | Notes |
|---|---|---|
| **`BELUGA` (36)** | **Nano Banana 2.1** | **default**; `gemini-nano-banana-2.1` |
| `BELUGA_THINKING_LOW/MED/HIGH` (37/38/39) | Nano Banana 2.1 (Thinking) | |
| `NARWHAL` (29) | Nano Banana 2 | deprecated; shuts down **2026-10-29** |
| `HARBOR_SEAL` (31) | Nano Banana 2 Lite | |
| `GEM_PIX_2` (25) | Nano Banana Pro | `gemini-3-pro-image` |
| `GEM_PIX_PRO_VERTEX`, `R2I` | (vertex / r2i) | present in enum |

VERIFIED output: a BELUGA generation returns `model_id: 36` in the parsed media.

### 4.2 Aspect ratio enum (integer)

`1:1→1, 9:16→2, 16:9→3, 3:4→4, 4:3→5` (also observed: `3:2→6, 2:3→7, 4:5→8,
5:4→9, 21:9→10`). Flow defaults to `16:9` (int 3) which renders at **1376×768**.

---

## 5. Image generation — the real path (VERIFIED end-to-end)

### 5.1 You cannot replay a fetch

reCAPTCHA Enterprise **scores the request flow/fingerprint**, not just the
payload. Proven:
- A console-minted token + pure-HTTP replay of the *byte-identical* captured
  `ogiZ0b` body → `PUBLIC_ERROR_UNUSUAL_ACTIVITY`.
- The **app's own composer request** (human-style click + type + Enter) → **200
  with media**, in the same session.

Therefore generation = **drive the composer and capture the app's response**.

### 5.2 The `ogiZ0b` payload (VERIFIED — exact from HAR 2026-10-11)

One `ogiZ0b` call carries **all N images** (count=2 → two request objects in
`[1]`), each with its own seed + uuid pair:

```
[ null,                                  # 0
  [ request, request, … ],               # 1  ← N requests (count)
  1,                                     # 2
  clientContext,                         # 3  same shape as request[7]
  [ batchUuid ]                          # 4  e.g. D1E3CC4F-CF2C-… (upper)

request[i] = [ null, null, null, <seed>, <aspectInt>, "BELUGA", null,
               clientContext,                # [null,22,null,null,null,<projectId>,
                                             #  null,null,null,null,["<recaptcha>",1]]
               [[[ "<prompt>" ]]],           # 8  structured prompt
               null, null, null,
               "<uuidUpper>", "<uuidUpper>" ]  # 12,13 — per-image uuid pair

clientContext = [ null, 22, null, null, null, <projectId>, null, null, null,
                  null, ["<recaptchaToken>", 1] ]
```

Verified from the real request: seeds `1424004164` / `125113416`, model string
**`"BELUGA"`** (the wire code, not the int), aspect `3` (16:9), recaptcha token
2,702 chars, per-image uuids uppercase. Response: media with
`[1376,768]` dims, prompt echo, and signed CDN URLs.

- Field is **`imageModelKey`** — the app sends the **string code** (`"BELUGA"`),
  not the numeric enum.
- recaptcha action for images = **`IMAGE_GENERATION`**.
- Success entry: `["<mediaUuid>", null, "<batchUuid>", …, [[null,<seed>,…,1,
  "<prompt>",36,…]], …]`; the signed download URL
  `https://flow-content.google/image/<cdnId>?Expires=…` is a **plain GET, no
  auth** — that's how images are downloaded.

### 5.3 What the engine actually does (generate())

1. **Session guard** — `session_ok()`; if logged out, `ensure_session()`.
2. Resolve project id; if none, **auto-create** (`jHPbke`) and persist it.
3. Navigate to `https://flow.google.com/project/<pid>`.
4. **Poll** for the composer `[contenteditable=true]` up to
   `FLOW_COMPOSER_TIMEOUT` (90 s) — Angular hydration is slow on 0.1 CPU.
5. Attach a `page.on("response")` listener; click the composer, clear leftovers,
   type the prompt (`delay=15`), press **Enter** (retry ≤3× if ignored).
6. Wait up to 90 s for the `ogiZ0b` response.
7. Parse the body for media uuids, seeds, prompt, `model_id`, signed URLs.
8. On `UNUSUAL_ACTIVITY` or no response → `restart()` once and retry (self-heal).
9. Return a list of media dicts.

### 5.4 Verified result (2026-10-10)

```
generate("a red fox in a snowy forest, cinematic lighting") -> 43.7 s
  model BELUGA (id 36), 2 media, seeds 686164503 / 399557315
  downloaded: 168,751 B and 186,799 B, both valid 1376×768 JPEG
auto-create path: generate() with NO project id ->
  created project 55ccb751-… , downloaded autocreated.jpg (304,346 B)
```

---

## 6. reCAPTCHA Enterprise & anti-detection

reCAPTCHA Enterprise flags the **automation fingerprint**, not the payload.

| signal | failing (headless) | passing |
|---|---|---|
| `navigator.webdriver` | `true` | `undefined` |
| UA brand | `"HeadlessChrome"` | `"Chromium"` / Chrome |
| `navigator.plugins.length` | `0` | `5` |
| `sec-ch-ua*` headers | leak `HeadlessChrome` | clean Chromium list |
| egress IP | datacenter | residential |

Mitigations implemented in `flow_headless.py`:
- `user_agent=SPOOF_UA` (Chrome 153 Linux desktop),
- `ignore_default_args=["--enable-automation"]`,
- `--disable-blink-features=AutomationControlled`,
- `add_init_script(STEALTH_JS)` — spoofs `webdriver`, `plugins`, `mimeTypes`,
  `languages`, `window.chrome`, permissions,
- `add_init_script(UAD_OVERRIDE_JS)` — replaces `navigator.userAgentData`,
- `ctx.route("**/*", _scrub_ch_headers)` — rewrites the `sec-ch-ua*` family on
  the wire,
- token mint via `grecaptcha.enterprise.execute(siteKey, {action})` **inside the
  real flow.google.com page**.

**Site key** = `WIZ_global_data.xZbWve` = `6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV`.
Mint verified: 2,425–2,489-char token in ~250 ms.

Verified fingerprint after the fixes:
`webdriver=null, plugins=5, UA Chrome/153, brands [Chromium 153, Not_A Brand 8]`.

**IP reputation:** reCAPTCHA scores the client IP. A datacenter IP
(Render/AWS/Cloudflare Workers) can be flagged even with a perfect fingerprint;
a residential IP passes. Mitigation: `FLOW_PROXY_URL` (route the browser through
a residential proxy) or run the engine on a residential connection.

---

## 7. Session & authentication

### 7.1 Cookies

Needed for `flow.google.com`: the `.google.com` family
`SID/HSID/SSID/APISID/SAPISID`, `__Secure-1PSID`, `__Secure-3PSID`,
`__Secure-1PAPISID`, `__Secure-3PAPISID`, plus the **rotating**
`__Secure-1PSIDTS` / `__Secure-3PSIDTS` (and often `__Secure-OSID`, `OSID`).
Export via Cookie-Editor (JSON array) or DevTools "Copy as cURL".

Import: `python import_cookies.py <file.json>` **or**
`python import_cookies.py --header "SID=…; HSID=…; …"` — writes
`sessions/flow.cookies.full.json` and prints a base64 blob for
`FLOW_COOKIES_JSON`.

### 7.2 Seed-once rule (critical)

Google **rotates `__Secure-1PSIDTS` every few minutes**. Re-injecting a static
export on every boot overwrites the browser's freshly-rotated token with a stale
one → Google treats the session as invalid → the app redirects to `/about`.

Therefore the engine **seeds cookies only once per profile** (marker file
`.cookies_seeded`), then leaves the profile alone so the browser keeps its
rotated session. Override with `FLOW_FORCE_COOKIES=1`; force a re-seed with
`restart(reseed=True)`.

### 7.3 Session health & self-heal

- `session_ok()` → an authenticated `nzlxg` read. `200` = logged in, `401` =
  logged out.
- `ensure_session()` → if logged out, re-seed cookies + rebuild once + retest.
- Logged-out signature in the UI: `flow.google.com` **redirects to `/about`**
  (the marketing page). That redirect is the tell.

### 7.4 Lifetime observed

A cookie export replayed from a **datacenter IP** worked for roughly
**10–30 minutes**, then went to `/about`. With the seed-once fix the browser
keeps its rotations and survives longer within a single instance. A durable
24/7 login needs a residential egress (§6) and/or a fresh-cookie push (§10).

---

## 8. Memory & the light engine (measured)

| engine | RSS with flow.google.com loaded | processes |
|---|---|---|
| full Chromium `--headless=new` | **~1.65 GB** | 9 |
| `chrome-headless-shell` | **~128–130 MB** | 1 |

Measured on this project's own engine: 87 MB idle → 128 MB loaded → 1 process.
The light binary runs the whole Angular app and mints reCAPTCHA tokens fine.
Relevant launch flags: `--no-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`,
`--disable-blink-features=AutomationControlled`, `--js-flags=--max-old-space-size=<FLOW_JS_HEAP_MB>`
(default 224), plus background-throttling off.

**Fonts are mandatory.** Without a working fontconfig the renderer aborts on the
Flow project page with
`FATAL: third_party/skia/.../SkFontMgr_FontConfigInterface.cpp:163] Not implemented`
and the page target is killed. Fixed by installing
`fontconfig fonts-liberation fonts-dejavu-core` (pinned in the Dockerfile).

---

## 9. HTTP API surface

Server: `flow_server.py` (stdlib `ThreadingHTTPServer`, JSON, CORS `*`).
Auth: `Authorization: Bearer <key>` from `API_KEYS` (`name:secret,...`).
Rate limits: `RATE_POST_PER_MIN` (20) / `RATE_GET_PER_MIN` (120) per identity.
GETs are open; POSTs require a key when `API_KEYS` is set.

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | fast, non-blocking; engine + session status |
| GET | `/diag` | **auth-gated**; real self-test: engine→load→fingerprint→mint→credits + verdict |
| GET | `/session` | detailed login state + login steps if dead |
| GET | `/restart` | hard rebuild (kill browser, fresh worker) |
| GET | `/credits` | remaining credits |
| GET | `/history?…` | project media (live, else cached mirror, flagged stale) |
| GET | `/projects` | list projects (`UpteDb`) |
| GET | `/project` | current project id |
| GET | `/project/new` | **create a project** (`jHPbke`) |
| GET | `/jobs`, `/jobs/<id>` | job queue + results (sqlite) |
| GET | `/logs?limit=&day=` | daily append-only JSONL |
| GET | `/media/<uuid>` | image bytes |
| GET | `/ui` | static test-bench page |
| POST | `/generate` | `{prompt, model?, aspect?, seed?, projectId?, count?}` → 202 job, or `?wait=1` for sync 200 |
| POST | `/upscale` | `{mediaId, resolution?}` → 202 job |
| POST | `/cookies` | push a fresh cookie export → re-seed + rebuild (no redeploy) |
| POST | `/project` | switch current project |
| POST | `/worker/*` | legacy home-worker endpoints (split-brain mode) |

`/health` fields: `ok, engine_alive, engine_responsive, engine, rss_mb, project,
model, busy, local_run, cookies_ready, proxy, session_ok, last_recover_ago_s,
boot_error, last_error, last_engine_ok_age_s`.

`/diag` returns `steps.{engine,proxy,load,fingerprint,mint,credits}` + a
`verdict` that names the exact failing stage.

---

## 10. Reliability / self-healing design

1. **Supervisor** (facade-owned, every `FLOW_SUPERVISE_S`, default 90 s):
   cheap liveness probe → hard rebuild on wedge/death; **skips rebuild while a
   job runs** (no thrash).
2. **Session self-heal**: periodic login check (every ~10 min) → re-seed +
   rebuild if lapsed; also invoked inline at the start of `generate()`.
3. **Startup**: bind port immediately → boot engine in background → start
   supervisor → verify login async.
4. **Single-flight rebuilds** guarded by a lock; wedged worker abandoned and
   replaced; stray browser processes SIGKILLed.
5. **Job pipeline**: sqlite-backed queue; jobs persist; slow ops never block the
   HTTP server (poll `/jobs/<id>`).
6. **Retries**: HTTP retries with backoff; stale `at` refresh; composer re-press;
   recaptcha-flag → restart+retry.
7. **Honest telemetry**: every failure path records a human-readable
   `last_error`; `/health` and `/diag` surface it.
8. **Auto project**: generation creates a project when none exists.

---

## 11. Configuration (environment variables)

| Var | Default | Purpose |
|---|---|---|
| `FLOW_ENGINE` | `headless` | `headless` = light engine |
| `FLOW_PROFILE` | `./profile-copy` | browser profile (holds the session) |
| `FLOW_OUT` | `./outputs` | output dir |
| `FLOW_PROJECT` | — | default project uuid (auto-created if unset at generate) |
| `FLOW_MODEL` | `NARWHAL` | default model wire code |
| `FLOW_COOKIES_JSON` | — | cookie export (JSON array or base64) |
| `FLOW_FORCE_COOKIES` | — | `1` = re-seed even if the marker exists |
| `FLOW_PROXY_URL` | — | residential egress `http://user:pass@host:port` |
| `FLOW_JS_HEAP_MB` | `224` | V8 old-space cap (renderer) |
| `FLOW_COMPOSER_TIMEOUT` | `90` | composer hydration wait (s) |
| `FLOW_SUPERVISE_S` | `90` | supervisor interval (s) |
| `FLOW_MEM_GUARD_MB` | `420` | soft RSS budget |
| `FLOW_CHROME` | auto | browser binary path |
| `API_KEYS` | — | `name:secret,...` |
| `RATE_POST_PER_MIN` / `RATE_GET_PER_MIN` | `20` / `120` | rate limits |
| `FLOW_MIN_SUBMIT_INTERVAL` | `45` | Google-facing throttle (s) |
| `FLOW_MAX_GENERATE_PER_DAY` / `FLOW_MAX_UPSCALE_PER_DAY` | `200` / `100` | daily caps |
| `PROFILE_TAR_URL` | — | logged-in profile tarball (seeded on boot) |
| `PORT` | `8787` | listen port (Render sets it) |
| `S3_*` | — | optional S3-compatible blob mirror |

---

## 12. Deployment

### 12.1 Render free (single path)

- **One Dockerfile** (`./Dockerfile`), referenced by `render.yaml`
  (`dockerfilePath: ./Dockerfile`). It installs **only**
  `chromium-headless-shell` + font packages.
- `plan: free` (512 MB), binds port instantly, engine boots in background.
- No persistent disk → the login is seeded from `FLOW_COOKIES_JSON` /
  `PROFILE_TAR_URL` on each boot.
- Free instances sleep after 15 min idle; keep warm with UptimeRobot on `/health`
  (750 instance-hours/month ≈ one always-on service).

> The repo previously shipped two divergent Dockerfiles (`Dockerfile.headless`,
> `Dockerfile.hosted`) and a `render.yaml` pointing at the wrong one (which
> installed full Chromium) — a direct cause of "blackout"/OOM deploys. Fixed by
> deleting them and keeping one canonical `Dockerfile`.

### 12.2 Residential option (recommended for 24/7)

Run the engine on a home machine (residential IP + more RAM) and expose it with
**Cloudflare Tunnel** (`cloudflared`) — free, no open ports. Cloudflare
**Workers are NOT a substitute**: they egress from datacenter IPs, so they don't
fix reCAPTCHA IP reputation, and they can't cleanly tunnel a whole browser.

---

## 13. Verified capabilities

| Capability | Status | Proof |
|---|---|---|
| Auth via cookies | ✅ | `nzlxg` 200 → credits 50 |
| `/credits` | ✅ | `[50,3,8,1,null,50,…]` |
| List projects | ✅ | `UpteDb` → project uuids + names |
| Create project | ✅ | `jHPbke` → `a80f9be1-…`, `55ccb751-…` |
| reCAPTCHA mint | ✅ | 2,425–2,489-char token |
| **Image generation** | ✅ | BELUGA, 2 media, JPEG 168 KB + 187 KB, 43.7 s |
| Auto-create + generate | ✅ | no project id → created + `autocreated.jpg` 304 KB |
| Download (signed URL) | ✅ | plain GET, byte-identical |
| Light engine fits 512 MB | ✅ | 128–130 MB, 1 process |
| `/diag` verdict | ✅ | names failing stage |
| Session self-heal | ✅ | detected dead session, re-seeded, reported |
| History / upscale (`Zzl0ze`,`SPrCad`) | ✅ (earlier) | 2K = exact 2× |

### Known issues / open items

- `projects()` via our own `UpteDb` call returned `[]` in one test while the app's
  own call listed projects — payload/parse nuance to confirm (app payload
  `["projects/*",21,null,null,null,null,[1]]` verified from HAR; our parse
  expects `rec[1][3]`/`[4]` which only exist for projects with media).
- Video tab (Omni / Veo) not wired — no video-generation RPC in this HAR.
- 4K upscale is paid-tier gated (G1_FREEMIUM).
- Sign-in RPCs (`MI613e`/`B4hajb`) are mapped but intentionally driven via the
  real UI (identity endpoints are fingerprinted; a raw replay would risk the
  account).

---

## 14. Files

```
flow_headless.py       light engine: stealth, mint, RPCs, composer-generate, session guard
flow_api_headless.py   facade: worker thread, supervisor, self-heal, jobs, /diag
flow_server.py         HTTP API (endpoints above)
flow_store.py          sqlite jobs/media index + logs
import_cookies.py      cookie importer → sessions + FLOW_COOKIES_JSON blob
start.sh               entrypoint: find browser, seed profile, run server
Dockerfile             single canonical image (light browser + fonts)
render.yaml            Render blueprint (free, ./Dockerfile)
flow_api.py            OLD full-Chromium engine (compat only)
worker.py/gen.py/menu.py/flow.py   CLI/REPL + split-brain worker
ui/index.html          static test bench
DEPLOY-FREE.md · README.md · CONTEXT.md · VERDICT.md · FLOW-INTERNALS.md (this)
```

---

## 15. Fixes found in this reverse-engineering pass (2026-10-10/11)

1. **`/u/1/` removed** — real endpoint is `/_/AiSandboxAngularFrontend/…`;
   project route is `/project/<id>`. (Was causing 400s and 404s.)
2. **`x-same-domain: 1` header** + `at` must carry `:timestamp`.
3. **Copy-the-headers RPC** — capture the app's URL/at live.
4. **Seed cookies once** — stop clobbering rotated `__PSIDTS`.
5. **Fontconfig/fonts required** — else the renderer aborts (Skia fatal).
6. **`/project/<id>` route** (was `/u/1/project/<id>`).
7. **Reverse-engineered `jHPbke` CreateProject + `UpteDb` ListProjects**.
8. **Single Dockerfile** (was three, one wrong); fonts pinned.
9. **Supervisor + session self-heal**; removed duplicate watchdog.
10. **`/diag`, `/cookies`, `/project/new`, `/projects`** added.
11. **HAR pass (2026-10-11)**: exact `ogiZ0b` multi-image payload; `jHPbke`
    named-create payload; full sign-in RPC map (`UEkKwb`→`MI613e`→`B4hajb`,
    password-only account); **auto-login implemented** (`engine.login()` via the
    real UI, chained into `ensure_session()` behind `FLOW_EMAIL`/`FLOW_PASSWORD`).
