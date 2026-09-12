# Hosting the always-live API (Render) + home worker

Architecture: **Render runs the API brain** (queue, media library, logs,
rate limits — no browser). **Your PC runs `worker.py`** (owns Chrome +
Google session, polls for jobs, uploads results). This split is required:
the engine needs your logged-in profile and cannot live on a server.

## 1. Push this folder to your GitHub (connected to Render already)

```powershell
cd D:\NIM-AAPI-SOLUTION-THROTLLER\flow-reversed
git init; git add -A; git commit -m "flow api"
git branch -M main
git remote add origin https://github.com/websitecage-hub/flow-api.git
git push -u origin main
```
(`sessions/`, `profile-copy/`, `outputs/`, `.env` are git-ignored and never
leave your PC. Create the empty `flow-api` repo on GitHub first.)

## 2. Create the Render service

Option A — dashboard: New → Web Service → select `flow-api` repo →
Runtime **Docker** → plan **Starter or higher** (free sleeps; always-live
needs paid) → Add env vars below → Deploy.

Option B — blueprint: same repo → New → Blueprint → picks `render.yaml`,
then fill the `sync: false` values.

Required env on Render:
```
ROLE=api
FLOW_PROJECT=<your project uuid from sessions/flow_api_state.json>
API_KEYS=cms1:<secret1>,cms2:<secret2>     # keys your content systems send
```
Recommended: `S3_ENDPOINT/BUCKET/KEY/SECRET/PUBLIC_BASE` (Cloudflare R2,
free 10GB) so images survive redeploys with public URLs. Without it,
attach a Render Disk mounted at `/app/outputs` (+ `/app/sessions`).

## 3. Run the worker at home (after every reboot / as scheduled task)

```powershell
cd D:\NIM-AAPI-SOLUTION-THROTLLER\flow-reversed
$env:API_BASE="https://flow-api-XXXX.onrender.com"; $env:API_KEY="<secret1>"
.\.venv\Scripts\python.exe -u worker.py --name home-pc
```
It heartbeats (`/workers` shows it), claims queued jobs, enforces the
Google throttle (45s gaps, daily caps, circuit breaker), uploads results.

## 4. Use from content systems

```
POST {API}/generate  Authorization: Bearer <secret1>
  {"prompt":"...","count":1,"aspect":"16:9","model":"NARWHAL"}
  -> 202 {"job":"gen-...","poll":"/jobs/gen-..."}
GET  {API}/jobs/gen-...            -> {status: queued|running|done|failed, result:{files,media,...}}
GET  {API}/history | /credits | /media/<uuid> | /logs | /health | /workers
```
Limits: 20 POST/min + 120 GET/min per key (429 + Retry-After); Google side
throttled in the worker regardless of how fast you submit.

## Notes

- RENDER_API_KEY in local `.env` is only for account inspection; never
  commit it (git-ignored).
- Free Render sleeps → queued jobs wait until it wakes; Starter+ stays live.
- Session still lives in your home profile; if Google logs it out, the
  worker logs scream and `GET /session` lists manual re-login steps.
