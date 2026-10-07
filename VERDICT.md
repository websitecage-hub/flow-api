# Flow API — verified verdict (2026-10-07)

Everything here was tested live against `flow.google.com` with a real
signed-in account. Where it says "verified" there is a captured response.

## What works (verified)

| Capability | Status | Cost |
|---|---|---|
| Auth via cookies (`FLOW_COOKIES_JSON`) | ✅ verified | — |
| `/credits` (`nzlxg`) | ✅ 50 credits | free tier |
| `/history` (`Zzl0ze`) | ✅ 8 items | free tier |
| `/app_config` (`cPZSdc`) | ✅ build `2026-10-06-v0-nb21-…` | free tier |
| reCAPTCHA token mint | ✅ 2489-char token | free tier |
| `ogiZ0b` request **reaches Google** | ✅ 200 | free tier |
| **Image generation** | ⚠️ see wall | needs real browser |

Engine: `chrome-headless-shell`, ~115–120 MB RSS — fits Render free 512 MB.
(`flow_api.py`'s old full-Chrome engine measured **1213 MB**; that number is
why the repo wrongly believed free tier OOMs.)

## The fingerprint wall (the honest blocker)

reCAPTCHA Enterprise rejects headless/automated browsers on the *write* RPC:

```
ogiZ0b -> 200  google.rpc.ErrorInfo  PUBLIC_ERROR_UNUSUAL_ACTIVITY
```

This is **not** a payload bug. Proof chain, all measured:

1. `chrome-headless-shell` (headless) — the **app's own composer**, a real
   human-style click+type+Enter, returns `UNUSUAL_ACTIVITY`. ~116 MB.
2. Full Chromium `--single-process`, headless — same. ~531 MB.
3. Full Chromium, normal multi-process, headless — the composer **succeeds**
   and returns a real image (media `1cb7192d-…`). But a non-composer
   `fetch()` of the *byte-identical captured request* is **still** flagged in
   the same session. ~1.4 GB.

So: the payload is right (the app accepts it when *it* sends it), the token is
right (minted by the app's own `grecaptcha`), and the `at` token is right (the
same session) — yet a programmatic `fetch` is rejected while the app's own
internal send passes. reCAPTCHA Enterprise is scoring the request origin/flow,
not merely the browser.

## What this means for the goal

"Run generation on Render free (512 MB), HTTP-only, no UI" is blocked by
Google's bot defence, not by our code or by RAM. Two viable paths:

- **A. Real browser for generation.** Full Chromium passes. It needs ~1.4 GB,
  so it runs **on your PC** (`worker.py` — the old split-brain) or on a paid
  box, not on free tier. The free Render API stays as the queue + reads.
- **B. Fingerprint work.** A non-headless, fully fingerprinted browser
  (camoufox / undetected-chromedriver style) that also reproduces the app's
  in-app XHR. Unproven; reCAPTCHA Enterprise is hard and this may never pass
  reliably.

Reads (history/credits/config/options) run fine on free tier today.

## Verified wire facts (so nobody re-guesses)

- `at` token = `WIZ_global_data.SNlM0e` (NOT `AIQ-…`).
- reCAPTCHA action for images = **`IMAGE_GENERATION`** (not `GENERATE`).
- `ogiZ0b` payload = `[null, [request…], 1, clientContext, [batchId]]`;
  `request = [null,null,null,seed,aspect,imageModelKey,null,clientContext,
  [[[prompt]]],null,null,null,<UUID>,<UUID>]`.
- field is **`imageModelKey`** (not `imageModelName`); token at
  `clientContext[10]`, not `[9]`.
- aspect ints: `1:1→1, 9:16→2, 16:9→3, 3:4→4, 4:3→5`.
- models: `NARWHAL=29` (Nano Banana 2, dies 2026-10-29), `HARBOR_SEAL=31`,
  `GEM_PIX_2=25`, and **`BELUGA=36` = Nano Banana 2.1** (+
  `BELUGA_THINKING_LOW/MED/HIGH`). Default = `BELUGA`.