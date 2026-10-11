# Deploy Flow API on Render FREE tier (512 MB) — self-contained

The whole API, browser engine included, runs on Render's **free** plan:
one service, one Dockerfile (`./Dockerfile`), pointed at by `render.yaml`.

## Why this fits 512 MB (measured on x86_64 with this engine)

| engine | RSS with flow.google.com loaded | processes |
|---|---|---|
| full Chromium, `--headless=new` (old `flow_api.FlowEngine`) | **1.65 GB** | 9 |
| `chrome-headless-shell` (`flow_headless.py`) | **~128 MB** | 1 |

`chrome-headless-shell` is Chrome's *lightweight* single-process headless
binary. It runs the whole Flow app and mints reCAPTCHA Enterprise tokens
(verified: 2.4 kB token). Full Chromium OOM-kills free tier — which is why the
repo once wrongly concluded "free tier is impossible". `render.yaml` →
`./Dockerfile` installs **only** `chromium-headless-shell`, never `chromium`.

## 1. Push

```bash
git add -A && git commit -m "free-tier headless engine"
git push
```

## 2. Create the service (blueprint)

Render → **New → Blueprint** → pick this repo → it reads `render.yaml`
(`plan: free`, `dockerfilePath: ./Dockerfile`, `FLOW_ENGINE=headless`).

Then fill the `sync: false` values:
- `FLOW_PROJECT` — your Flow project uuid
- `API_KEYS` — e.g. `cms1:<a-long-random-secret>`
- `FLOW_COOKIES_JSON` — base64 cookie export (see §4)

Optional: `PROFILE_TAR_URL` (see §4), `FLOW_PROXY_URL` (see §7).

## 3. Keep it awake

Free instances sleep after 15 min idle (~730 h/month ≈ one always-on service;
the free allowance is 750 h). Point an UptimeRobot HTTP monitor at
`https://<service>.onrender.com/health` every 5 minutes.

## 4. The one thing free tier can't do: keep a login

Render free instances have **no persistent disk**, so the logged-in browser
profile is wiped on every deploy/restart. Pick whichever is easiest for you.

### Option A — cookies only (BEST FROM A PHONE)

No PC needed. You need the Google cookies for an account already signed in to
flow.google.com. Get them on your phone:

1. Install a cookie-export extension in your phone browser (e.g. "Cookie-Editor"
   in Kiwi/Firefox for Android; on iOS use a desktop-mode browser), or open
   DevTools remotely.
2. Go to `https://flow.google.com` (signed in) and **Export cookies as JSON**.
   You must include the `SID/HSID/SSID/APISID/SAPISID` family plus
   `__Secure-1PSID`, `__Secure-3PSID` and `__Secure-next-auth.session-token`.
3. Turn it into the env value:

   ```bash
   python import_cookies.py devtools-cookies.json
   # prints a base64 blob -> paste into Render env FLOW_COOKIES_JSON
   ```

   Or, straight from a "Copy as cURL" cookie header:

   ```bash
   python import_cookies.py --header "SID=...; HSID=...; SSID=...; ..."
   ```

4. Set `FLOW_COOKIES_JSON` on Render (base64 is fine — the engine decodes it).
   On boot it injects them; reads (history/credits/options) then work.

> Reads work from cookies alone. Generation may additionally require the
> profile's reCAPTCHA/account state — if `/generate` still errors after cookies,
> use Option B for a stored profile.

### Option B — full profile tarball

Needs one machine where you logged in to flow.google.com once:

```bash
# profile dir is FLOW_PROFILE (default ./profile-copy)
tar -czf profile.tgz -C profile-copy .
# upload profile.tgz somewhere private, set PROFILE_TAR_URL to its URL
```

`start.sh` unpacks it into `FLOW_PROFILE` on boot.

Either way, treat cookies/tarball as a **secret** (live Google session). Never
commit them.

## 5. Verify (this is the important part)

```bash
curl https://<service>.onrender.com/health
# {"ok":true,"engine":"chrome-headless-shell","rss_mb":129.8,
#  "engine_alive":true,"cookies_ready":true,"proxy":false,
#  "boot_error":"","last_error":""}

curl -H "Authorization: Bearer <secret>" https://<service>.onrender.com/diag
```

`/diag` runs a real engine self-test and returns a **verdict** naming the
exact failing stage:

```json
{"steps":{"engine":{"ok":true,"rss_mb":129.8},
          "load":{"ok":true,"url":"https://flow.google.com/u/1/project/<id>"},
          "fingerprint":{"ok":true,"webdriver":null,"plugins":5},
          "mint":{"ok":true,"token_len":2425},
          "credits":{"ok":true,"credits":50}},
 "verdict":"logged in + minting. If /generate still returns UNUSUAL_ACTIVITY,
            egress IP is flagged — set FLOW_PROXY_URL to a residential proxy."}
```

`/health.last_error` also shows why the last `/generate` failed.

## 6. Use it

```bash
curl -X POST https://<your-service>.onrender.com/generate \
  -H "Authorization: Bearer <secret>" -H "Content-Type: application/json" \
  -d '{"prompt":"a red fox in snow","aspect":"16:9","count":1}'
# -> 202 {"job":"gen-...","poll":"/jobs/gen-..."}
curl -H "Authorization: Bearer <secret>" https://<your-service>.onrender.com/jobs/gen-...
```

## 7. If generation returns PUBLIC_ERROR_UNUSUAL_ACTIVITY

reCAPTCHA Enterprise scores the **client IP**. Render runs on datacenter IPs
(AWS), which can be flagged even with a perfect browser fingerprint — while
your home (residential) IP passes. `/diag` will show `"mint":{"ok":true}` but
`/generate` still gets `UNUSUAL_ACTIVITY`.

Fix: route only the browser through a residential proxy:

```
FLOW_PROXY_URL=http://user:pass@residential-host:port
```

Set it in the Render dashboard (sync:false). `/health.proxy` then reads `true`.
Nothing else changes.

## Notes / limits

- Free instances **sleep after 15 min idle** and cold-start in ~20–40 s (the
  browser has to boot). First call after a sleep will be slow; queued jobs
  just wait. If you need always-on, Starter ($7) keeps it warm.
- The engine is ~120 MB; keep an eye on `/health.rss_mb`. `FLOW_MEM_GUARD_MB`
  (default 420) is the soft budget.
- Google-facing throttle still applies (`FLOW_MIN_SUBMIT_INTERVAL=45 s`,
  daily caps) — this protects the account, not the server.
- Architecture note: these numbers are x86_64. Render free is x86_64, so fine.
  If you ever move to an ARM free host (e.g. Oracle Ampere), install the
  arm64 `chrome-headless-shell` (`playwright install chromium-headless-shell`
  on that box).