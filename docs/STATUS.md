# Flow API — Where We Are, and What Flow Actually Requires

*Written 2026-11-09, for anyone (including future-us) who needs to understand
this system in ten minutes without the two weeks of debugging behind it.*

---

## 1. What this service is

A self-contained API that generates images through **Google Flow**
(flow.google.com) and serves them watermark-free, deployed on Render's **free
tier** (512 MB RAM, no persistent disk).

Live URL: https://flow-api-a9ei.onrender.com
Repo: https://github.com/websitecage-hub/flow-api (branch `main`)

A user (or the built-in web UI) POSTs a prompt to `/generate`. A job is queued.
A single worker picks it up, drives a real Chrome browser to the Flow website,
types the prompt into Flow's own composer, clicks Flow's own generate button,
waits for Flow to render, harvests the images, strips the watermark from each
one, stores them, and reports the job done. Clients poll `/jobs/<id>` or read
the `/media` library.

---

## 2. The one thing that must be understood first

**We do not call an image-generation API. Google does not offer one for Flow.
There is no documented endpoint, no API key, no official SDK.**

What we have instead is a website designed for humans. So the service works by
**driving the website the way a human does**, through a protocol called CDP
(Chrome DevTools Protocol) — the same channel your browser's F12 debugger uses.
The engine is not a scraper of HTML; it is a robot sitting at a browser.

That single fact explains nearly every constraint below.

---

## 3. What Flow genuinely requires for one generation to work

### 3.1 A real, logged-in browser session

Flow is part of Google's ecosystem. The service holds a Google account session
as HTTP cookies (the important ones are named `SID`, `HSID`, `SSID`,
`__Secure-1PSID`, and friends) plus a short-lived CSRF token called `at` that
Flow's JavaScript reads from the page on every RPC.

- The cookie jar is minted at boot from a **master token** (`FLOW_MASTER_TOKEN`
  env var, the account's refresh-style token) — the free tier has no disk, so
  nothing survives a deploy and it must be re-seeded every boot.
- The `at` token **expires** and can only be re-read from a live, loaded page.
  This is why the browser must load Flow even when it "just" wants an RPC.

### 3.2 A hydrated page — the part people underestimate

Chrome can send HTTP requests all day without loading a page. Flow does not
accept that. Every generate click needs:

- Flow's **Angular application fully booted** in the page (14+ seconds of
  hydration), because
- the **`at` token** is injected into the page's JS world by that app, and
- the RPC is sent **from inside the page context** (`fetch('/_/…')`), so
  Google's own JavaScript, headers, and origin all line up — with cookies,
  browser TLS, and the same-origin guarantee intact.

Practical consequence: **you cannot generate from a cold Python process.**
The page must be open and hydrated at the moment of generation. This is why
the engine keeps a warm page open — it costs ~118 MB of RAM but makes each
generation take ~30–45 s instead of ~60–90 s.

### 3.3 A client IP that reCAPTCHA trusts

Every generate click is scored by **reCAPTCHA Enterprise**, silently, server
side. It weighs: the client IP's reputation, the browser fingerprint (which we
spoof heavily to look like normal Chrome), and behavioral signals (typing
cadence, etc.).

- Datacenter IPs (Render, AWS, GCP) score **worse** than home/residential IPs.
  This is not paranoia — we watched it happen live (see §5).
- If the score is too low, Google returns `PUBLIC_ERROR_UNUSUAL_ACTIVITY` and
  the generation silently produces nothing.

### 3.4 Enough RAM to hold a hydrated Angular app

The hydrated Flow tab in a full Chromium costs hundreds of MB. The whole
strategy of this deployment (§4) exists because of a single number: the free
tier gives us **512 MB**.

### 3.5 Patience on Flow's side

Once submitted, Flow itself takes 20–60 s to render. That part is Google's
compute and we cannot influence it. Our own overhead is ~5–15 s on top.

---

## 4. How we fit Flow into 512 MB — the engine, in one paragraph

The engine runs `chrome-headless-shell` (Google's stripped, single-process
headless browser — NOT full Chromium) driven over raw CDP sockets by pure
Python. No Playwright driver at runtime (its Node process alone eats ~120 MB).
One browser, one page, one generation at a time. A measured footprint:

```
idle:    ~167 MB total (49 python + 118 chrome, page warm)
render:  ~340 MB peak      (memory guard kills the browser at 495 MB)
```

Everything else in the service (HTTP server, job queue, session heal, image
watermark stripping) is deliberately small. This is the practical minimum that
can still render Flow images. Two lighter paths were tested and rejected:

- **Lightpanda browser** (a minimal Zig browser): loads Flow beautifully in
  85 MB — but Flow's composer **never renders** in it (0 input fields after
  50 s of hydration). Cannot type, cannot generate. Dead end today.
- **Pure-HTTP engine** (no browser at all, ~60–90 MB): would have to forge
  Flow's internal RPC from Python. Nobody has proven it passes reCAPTCHA; a
  non-browser TLS fingerprint is the classic bot tell. A research project,
  not an engineering one.

---

## 5. The war stories (what actually went wrong, and the fixes)

### 5.1 "Composer not found" (the session looked dead)

For weeks, jobs failed with `composer not found — not logged in?`. The real
bug: our CDP helper only unwrapped JavaScript arrow functions when an argument
was passed; bare arrows returned `{}` to Python. The token scraper therefore
got an empty string, every RPC failed, and the error message blamed the login.
**Fix:** always invoke callable results in `evaluate()` (commit e0e6a1c).

### 5.2 The session "died" every 10–40 minutes

Two independent writers were fighting over the session cookie `__Secure-1PSIDTS`:
Google's own in-browser rotation, and our server-side rotate-every-cycle logic.
Each overwrote the other, the cookie went stale, Google rejected it, and the
service concluded it was logged out — every ~10 minutes.
**Fix:** one rotator only. The browser is the source of truth; the server
*harvests* Google's fresh cookies each cycle instead of minting its own
(commits 5f5b230, a36248a, 1be103b). Baseline was 3 deaths/12 min; after: 1
death in 31 min (the boot on the old jar), then zero.

### 5.3 A 230-second job that produced nothing, then "disappeared"

The user's prompt hung for 230 s, a page refresh lost it, and later prompts
failed. The chain: reCAPTCHA **flagged Render's egress IP**
(`PUBLIC_ERROR_UNUSUAL_ACTIVITY`), the engine treated it as transient, restarted
Chrome (~50 s each time) and resubmitted — three cycles ≈ 230 s — and the UI
showed failures as a thin note that vanished on reload.
**Fixes:** an IP-flag cooldown with escalating back-off (5→10→20→40→60 min,
cleared by one success) instead of retries (0545c1b); `/health` exposes
`ip_flag`; the UI has a loud failure banner; a failed job now reports the real
reason; and the WARP standby exists for when rest is not enough (3e46367).
The flag itself cleared after a few hours of not hammering — the IP recovered,
confirming it is a *reputation* score, not a ban.

### 5.4 Slow generations (55 s+ of overhead)

Instrumentation showed 20.7 s of navigation and 11.9 s of typing per job —
against a 24 s actual render. **Fixes:** reuse the warm page (nav → 0 s),
batch keystrokes over CDP instead of one round-trip per character (typing →
~1.5 s), and replace fixed sleeps with real verification checks (bf0c55f,
7192ba9). Now: **31–42 s warm, 60–140 s cold.**

---

## 6. Current state (2026-11-09)

**Working:**
- Generation end-to-end: 31–42 s warm, watermark-free, via UI or API
- Session self-heals; a job that hits a lapse waits for the heal and re-runs
  instead of failing (the "session maxxing" package)
- IP-flag cooldown + escalating back-off + WARP standby (dormant, one env var)
- Flow-like web UI (tabs, bottom composer, resume-on-reload, loud failures)
- 321 invariant tests, all passing

**Known limits (accepted, for now):**
- **Aspect ratio:** output is always 16:9 (1376×768). The ratio is a
  *per-project UI setting* we could not reach by any of three tested paths
  (request field, settings panel, prompt clause). Parked until the user
  captures a HAR of manually setting it, which will reveal the real mechanism.
- **Watermark removal** is calibrated to exactly 1376×768; other sizes are
  passed through *with* watermark (logged loudly).
- **History/jobs vanish on deploy** — no disk on the free tier.
- **Image-to-image** (`/upload`) uploads fine but attach-to-generation was
  never captured — needs a HAR of that action too.

**Open operational notes:**
- API key is `Anshusingh99` (short/guessable — rotate before sharing widely)
- The Google account is a throwaway; rotate after testing
- UptimeRobot (or similar) should ping `/health` so Render never sleeps the
  free service (only ONE free Render service is allowed — do not duplicate)
- Cloudflare API token was pasted into chat once: **rotate it** (unused by us)

---

## 7. If you are picking this up cold — the 60-second version

1. It generates images by driving the Flow *website* in a real headless
   Chrome over CDP — because Google offers no API for Flow.
2. The page must be loaded and hydrated when generating; the engine keeps one
   warm page open (~118 MB). That is the RAM story.
3. Sessions are cookie jars minted from a master token at every boot (no
   disk); the browser rotates its own cookies and the server follows.
4. reCAPTCHA scores our egress IP on every generation; a bad score means
   backing off (automatic cooldown), resting, or switching on the WARP
   standby (`FLOW_WARP=1`).
5. Deploy is Render Blueprint (`render.yaml` → `./Dockerfile` → `./start.sh`);
   one service, free tier, 321 green tests, `/health` for the monitor.
