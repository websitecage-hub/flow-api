# Flow API — complete surface

Hosted service: `https://flow-api-a9ei.onrender.com` (Render free tier).
Auth: `Authorization: Bearer <secret>` where `<secret>` is **only** the part
after `cms1:` in `API_KEYS` — never the whole `cms1:secret` string.

`GET /models` is the live, self-documenting version of this file: it returns
every model, aspect, upscale target and endpoint straight from the engine's
verified tables. Prefer it over hard-coding anything.

## Generation

```
POST /generate
{
  "prompt": "required string",
  "model":  "optional — key or alias from GET /models",
  "aspect": "optional — e.g. 16:9, 1:1, 21:9 (see GET /models)",
  "seed":   123,                 // optional int, reproducible batch
  "projectId": "<uuid>",         // optional; auto-creates a project if omitted
  "count":  1
}
-> 202 {"ok": true, "job": "gen-...", "poll": "/jobs/<id>"}
-> 429 {"ok": false, "error": "a generation is already running",
        "retry_after_s": 30}                       // one job at a time
-> 400 {"ok": false, "error": "unknown model 'X'", "models": [...],
        "hint": "GET /models ..."}                 // validated, not defaulted
```

`GET /jobs/<id>` → `status: running | done | failed`, and on success a
`result.media[]` array with `uuid`, `batch`, `seed`, `prompt`, `model`,
`model_id` and a **signed** CDN `url` (expires in days — save the bytes).

Each call returns **2 images**. Jobs take ~150–400 s on the 0.1-CPU free tier.

## Models (mapped from Flow's own enum)

| key | label | id | notes |
|---|---|---|---|
| `BELUGA` | Nano Banana 2.1 | 36 | **default** |
| `BELUGA_THINKING_LOW` | Nano Banana 2.1 (Thinking Low) | 37 | |
| `BELUGA_THINKING_MED` | Nano Banana 2.1 (Thinking Med) | 38 | |
| `BELUGA_THINKING_HIGH` | Nano Banana 2.1 (Thinking High) | 39 | |
| `GEM_PIX_2` | Nano Banana Pro | 25 | `gemini-3-pro-image` |
| `NARWHAL` | Nano Banana 2 | 29 | deprecated, shuts down 2026-10-29 |
| `HARBOR_SEAL` | Nano Banana 2 Lite | 31 | |

Human aliases (`NANO BANANA 2.1 THINKING HIGH`, `NANO BANANA PRO`, …) are
accepted directly — `GET /models` lists them per model. `model_enum_all`
publishes the full 21-entry bundle enum (Imagen, GemPix, Vertex variants).

**Note:** requesting a Thinking variant returns `BELUGA` (36) in practice —
the account appears to be served the base model. Check `result.media[].model_id`
to see what was actually used.

## Aspect ratios

`1:1, 9:16, 16:9, 3:4, 4:3, 3:2, 2:3, 4:5, 5:4, 21:9` — wire ints in `GET /models`.

## Upscale

```
POST /upscale  {"mediaUuid": "<uuid>", "resolution": "2K" | "4K"}
```

## Accounts, projects, media

| endpoint | what |
|---|---|
| `GET /credits` | remaining credits (`50` observed) |
| `GET /whoami` | ⚠️ **experimental, disabled** — `o30O0e` payload unvalidated; it wedged the page, so it's gated behind `FLOW_EXPERIMENTAL=1` |
| `GET /projects` | project list (`UpteDb`) |
| `GET /project` | the active project id |
| `GET /project/new` | create a project (`jHPbke`) |
| `GET /history` | media for the active project (`Zzl0ze`), falls back to a local cache |
| `GET /media/<uuid>` | download the image bytes |
| `POST /upload` | ⬆ upload a reference image (`maseQ`) — see the upload section |
| `GET /media/<uuid>?info=1` | ⚠️ **experimental, disabled** — `as29s` returned nothing and wedged the page; gated behind `FLOW_EXPERIMENTAL=1` |

**Why the two are off:** both returned no data *and* left the renderer stuck for
~60 s (`Runtime.evaluate: timeout`), and that worker is the same one generations
use — an unvalidated call could stall a real job. They need a HAR capture of the
finished flows before their payloads can be trusted.

## Ops

| endpoint | what |
|---|---|
| `GET /health` | **monitor target** — always 200, engine + memory state in the body |
| `GET /models` | full catalogue (works even while a job runs) |
| `GET /diag` | staged self-test; returns `BUSY` rather than a false failure |
| `GET /session` | login state, jar source, `at` age, rotation status |
| `GET /jobs`, `GET /jobs/<id>` | job list / status |
| `GET /logs` | recent server logs |
| `GET /restart` | rebuild the browser |
| `POST /cookies` | seed a fresh cookie export |
| `GET /ui` | phone-friendly test bench |

`HEAD /health` and the trailing-slash form `/health/` are both supported, so
uptime monitors work whichever way they're configured.

## Console UI

Open **`/`** (or `/ui`) on the service — a single self-contained page, no CDN,
so it always loads. It shows live health (engine, busy, memory, session),
lets you generate with model/aspect/count, streams job progress, renders the
results as a gallery with download links, and gives one-click access to every
GET endpoint plus the full model catalogue.

Paste your API key once; it is kept in that browser's `localStorage` only.

## Auth for browsers

Programs send `Authorization: Bearer <key>`. That is impossible from an
`<img>` tag or a plain link, which is why opening a media URL directly looked
broken. The service therefore also accepts **`?key=<secret>`**:

```
/media/<uuid>?key=<secret>        renders in a browser, downloadable
```

Same key, same validation; the query form exists purely so the UI and plain
links work. Prefer the header from servers (query strings can land in logs).

## Watermark

Every render carries Google's **visible 4-pointed sparkle**, alpha-composited
white at a fixed spot (~100 px in from the bottom-right of a 1376x768 image).
It is faint — nearly invisible on bright backgrounds, obvious on dark ones.

There is **no server-side control** for it: no request field, config key or UI
toggle (re-confirmed against traffic and the app UI). It is applied by Google.

`dewatermark.py` removes it by inverting the composite
(`img = (out - 255*a)/(1-a)`), deriving `a` from the per-pixel minimum across
many renders. Verified visually on bright *and* near-black backgrounds: glyph
gone, pixels preserved, no smearing.

```
python dewatermark.py                      # clean every file in outputs/
python dewatermark.py in.jpg out.jpg       # one file
```

The invisible **SynthID** signal and the signed **C2PA/IPTC** provenance are
*not* removable by this (nor by anything short of destroying the image) —
provenance survives regardless.

## Upload / reference images — MAPPED (wire captured and decoded)

Captured by driving the real UI (not guessed). The upload is **not** a separate
multipart endpoint and has nothing to do with `flow-content.google`: the image
bytes ride **inline in a normal batchexecute call**, RPC **`maseQ`**.

UI path: composer → **"Add ingredients to the prompt box"** → menu →
**"Upload media"**. That opens the *native* file chooser (there is no
`<input type=file>` in the DOM), so automation must either intercept
`Page.fileChooserOpened` / use Playwright's `expect_file_chooser`, or drive it
some other way. After the file is handed over, the app shows a consent gate
(**"I agree"**) and an **"Add to prompt"** action; the media then appears under
an **"Uploads"** collection with an **"Ingredient"** chip on the prompt.

Request:

```
POST /_/AiSandboxFrontend/data/batchexecute?rpcids=maseQ&source-path=/project/<id>
content-type: application/x-www-form-urlencoded;charset=utf-8
body: f.req=<urlencoded JSON>&at=<xsrf>
```

Decoded payload (base64 collapsed):

```json
[
  [null, 22, null, null, null, "<projectId>", null, null, null, null,
   ["<2553-char token>", 1]],
  "<base64 of the raw image bytes>",     // /9j/4AAQ… == JPEG SOI on decode
  "image/jpeg",                          // mime
  1,
  null, null, null, null,
  "probe_upload.jpg",                    // filename
  null,
  "<uuid v4 upper>",
  "<uuid v4 upper>"
]
```

Field positions verified from the capture: project id at `[0][5]`, mime at
`[2]`, filename at `[8]`, two generated UUIDs at `[10]`/`[11]`. Index `[1]` of
the first element is the literal `22`; the 2553-char token at `[0][10][0]` is
still unidentified in origin (may or may not be required).

**Implementation note:** the upload is issued from inside the page
(`JS_RPC`-style fetch), the same way every other RPC in this service works.
Do **not** build it on pure-HTTP batchexecute — see the transport finding
below; that path is currently unverified.

### `POST /upload` — implemented

Two body forms are accepted:

```
# 1. JSON (base64) — what the console UI sends
curl -X POST https://flow-api-a9ei.onrender.com/upload \
  -H "Authorization: Bearer <secret>" -H 'Content-Type: application/json' \
  -d '{"image_base64":"<base64>","filename":"ref.jpg","mime":"image/jpeg"}'

# 2. raw bytes — no base64 step
curl -X POST "https://flow-api-a9ei.onrender.com/upload?filename=ref.jpg" \
  -H "Authorization: Bearer <secret>" -H 'Content-Type: image/jpeg' \
  --data-binary @ref.jpg
```

```
-> 200 {"ok": true, "media_uuid": "<uuid>", "mime": "image/jpeg",
        "filename": "ref.jpg", "project": "<uuid>", "bytes": 182134,
        "uuids": [...], "urls": [...]}
-> 400 {"ok": false, "error": "image required", "hint": "...", "max_mb": 4}
-> 413 {"ok": false, "error": "image too large (5.2 MB decoded)", "max_mb": 4}
-> 429 {"ok": false, "error": "a job is already running", "retry_after_s": 30}
```

`filename` is optional (auto-named from the sniffed type); `projectId`
defaults to the active project, auto-creating one if unset. The MIME is
sniffed from the JPEG/PNG/WebP magic when not supplied.

**Scope, stated plainly:** this performs the *upload* — the image lands in the
project's **Uploads** as a prompt ingredient, exactly as the app's own
"Upload media" flow does. **Attaching an uploaded image to a generation is not
wired**: the phase-2 request that references an ingredient was never captured,
so image-to-image is still impossible through this API. The size ceiling
(`FLOW_MAX_UPLOAD_MB`, default 4) exists because the bytes are base64'd and
urlencoded on the way to Google — several times the file size in peak memory,
on a container shared with the browser.

### Open issue: pure-HTTP batchexecute returns 400

A control test proved this is **not** upload-specific. Sending `nzlxg`
(credits) — an RPC the app itself calls successfully — from Python returns:

```
[["er",null,null,null,null,400,null,null,null,3], ...]   → HTTP 400
```

and it fails identically with: the token from `harvest_at` *and* from
`WIZ_global_data.SNlM0e` (both verified present, 42 chars, colon+timestamp,
correct shape); `source-path` both `/` and `/project/<id>`; and the browser's
exact header set (content-type, referer, UA, `x-same-domain: 1`, client hints —
the capture shows the browser sends **no** `x-goog-ext-*` headers and no
`Origin`). Cookies are fine over HTTP — an authenticated `GET /` succeeds.

So in-page `JS_RPC` is the proven path and the only one to build on. Whatever
remains in the pure-HTTP path is unresolved and must not be assumed to work.

## RPC map (wire level, behind the endpoints)

`nzlxg` credits · `maseQ` **upload media** (inline base64 image) ·
`UpteDb` list projects · `jHPbke` create project ·
`Zzl0ze` project history · `ogiZ0b` batch-generate images · `SPrCad` upscale ·
`as29s` media detail · `o30O0e` profile · `cPZSdc` app config ·
`Yizz8d` home defaults · `ngNC2` generation defaults · `HTrJv`/`tRARke` app +
model config (33 KB / 47 KB) · `yBhWQ` video-model availability ·
`xI9TVb` preferences. See `FLOW-INTERNALS.md` for payload shapes.

Unmapped so far (present in the app, not yet exercised): video generation
(`yBhWQ` exposes availability only), and the `LPzVkd`/`qJcgMc`/`mrlkwd`/
`tRARke`/`ve2Lsc` config/data feeds.
