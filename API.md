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

## Upload / reference images — not mapped yet

Flow's image-to-image ("upload a reference") path is **not implemented**,
because the upload RPC has never been captured. Mapping it needs a HAR of the
live flow:

1. In the Flow UI, start a generation and **attach/upload an image**.
2. Capture the whole session (DevTools → Network → *Save all as HAR*).
3. What's needed from it: the upload request (endpoint + method + form fields,
   likely to a `flow-content.google` / `labs.google` host), the returned asset
   id/URL, and the `ogiZ0b` request showing **where that asset id is injected**
   (the reference-image slot in the payload).

Guessing those fields would be worse than not having the feature — the same
class of mistake that made the cookie bug take days. Until then, `/generate` is
text-to-image only.

## RPC map (wire level, behind the endpoints)

`nzlxg` credits · `UpteDb` list projects · `jHPbke` create project ·
`Zzl0ze` project history · `ogiZ0b` batch-generate images · `SPrCad` upscale ·
`as29s` media detail · `o30O0e` profile · `cPZSdc` app config ·
`Yizz8d` home defaults · `ngNC2` generation defaults · `HTrJv`/`tRARke` app +
model config (33 KB / 47 KB) · `yBhWQ` video-model availability ·
`xI9TVb` preferences. See `FLOW-INTERNALS.md` for payload shapes.

Unmapped so far (present in the app, not yet exercised): video generation
(`yBhWQ` exposes availability only), and the `LPzVkd`/`qJcgMc`/`mrlkwd`/
`tRARke`/`ve2Lsc` config/data feeds.
