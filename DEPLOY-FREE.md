# Deploy Flow API on Render FREE tier (512 MB) — self-contained

This replaces the "split-brain" setup (paid Render + a home PC worker). The
whole API, browser engine included, now runs on Render's **free** plan.

## Why the old conclusion was wrong

The repo said free tier OOMs and you need a paid plan plus a home worker.
That was measured with full Chrome in `--headless=new` mode, which runs an
~11-process renderer tree. Measured on a Linux x86_64 box:

| engine | RSS with flow.google.com loaded |
|---|---|
| full Chrome, `--headless=new` (old) | **1213 MB** |
| `chrome-headless-shell` (this) | **115 MB** (118 MB peak while minting) |
| + Python, Playwright, sqlite, server | ~125 MB total |

`chrome-headless-shell` is Chrome's *other* headless binary — the lightweight
one — built for exactly this. Single process. It runs the full Flow app and
mints reCAPTCHA Enterprise tokens just fine (verified: 2489-char token, 254 ms).

## 1. Push

```bash
git add -A && git commit -m "free-tier headless engine"
git push
```

## 2. Create the service (blueprint)

Render → **New → Blueprint** → pick this repo → it reads `render.yaml`
(already set to `plan: free`, `Dockerfile.headless`, `FLOW_ENGINE=headless`).

Then fill the two `sync: false` values:
- `FLOW_PROJECT` — your Flow project uuid
- `API_KEYS` — e.g. `cms1:<a-long-random-secret>`

Optional: `PROFILE_TAR_URL` — see step 3.

## 3. The one thing free tier can't do: keep a login

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

## 4. Verify

```bash
curl https://<your-service>.onrender.com/health
# {"ok":true,"engine":"chrome-headless-shell","rss_mb":123.0,...}

curl -H "Authorization: Bearer <secret>" \
     "https://<your-service>.onrender.com/session"
# session_ok:true once the profile is seeded
```

## 5. Use it

```bash
curl -X POST https://<your-service>.onrender.com/generate \
  -H "Authorization: Bearer <secret>" -H "Content-Type: application/json" \
  -d '{"prompt":"a red fox in snow","aspect":"16:9","count":1}'
# -> 202 {"job":"gen-...","poll":"/jobs/gen-..."}
curl -H "Authorization: Bearer <secret>" https://<your-service>.onrender.com/jobs/gen-...
```

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