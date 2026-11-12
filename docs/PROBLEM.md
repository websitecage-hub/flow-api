# PROBLEM.md — the exact compute problem, stated for any AI/engineer

*Companion to STATUS.md. Paste this file's contents anywhere to ask another
AI for solutions. Everything is measured, not guessed; dead ends are marked
so they don't get re-suggested.*

---

## PROBLEM STATEMENT — running a "website-as-API" image engine in 512 MB RAM

### GOAL
Serve an HTTP API that generates images through Google Flow
(flow.google.com) for content automations / AI agents, 24/7, at $0.

### THE HARD CONSTRAINTS (all verified by measurement)
1. **Google Flow has NO public API.** No endpoint, no SDK, no official key.
   The only interface is the website itself.
2. **Flow's generate RPC only works from INSIDE a loaded page:**
   - the page must be fully hydrated (Angular app boots ~14 s),
   - the CSRF `at` token is injected into the page by that app,
   - the RPC must run in the page's own JS context (same-origin fetch)
     with the session cookies intact.
   → a real browser must be open and hydrated at the moment of generation.
3. **Every generate click is scored server-side by reCAPTCHA Enterprise**
   (invisible, risk-score mode — there is never a challenge to solve).
   Datacentre IPs (Render/AWS/GCP) score worse than residential IPs.
   Low score → `PUBLIC_ERROR_UNUSUAL_ACTIVITY` → Google silently produces
   nothing. Measured live: a shared datacentre IP got flagged after heavy
   use, recovered after hours of rest, and a Cloudflare WARP egress passed
   an A/B test with zero flags (+56 s latency per generation).
4. **Hosting budget: Render FREE** = 512 MB RAM, 0.1 CPU, no persistent
   disk, no TUN device / NET_ADMIN (so no real WireGuard/WARP client
   in-container).
5. **Sessions:** cookie jar re-minted from a master token at every boot
   (no disk); Google rotates the PSIDTS cookie ~every 10 min.

### CURRENT ARCHITECTURE (works; deployed)
- `chrome-headless-shell` (Google's stripped single-process headless build),
  driven by a pure-Python raw-CDP client (no Playwright/Node at runtime —
  the Node driver alone cost ~120 MB).
- One browser, one page, one generation at a time; page kept warm.
- Self-healing session: 4-min keepalive, cookie harvest from the browser,
  heal with in-place restore, wipe of the persisted cookie store on
  CookieMismatch, escalating IP-flag cooldown (5→60 min).
- Result: ~31–45 s per generation warm, watermark-free output.

### MEASURED RAM FOOTPRINT (the actual compute problem)
```
idle:            ~167 MB total   (49 MB python + 118 MB chrome, warm page)
during a render: ~340 MB peak    (hydrated Angular app in the renderer)
ceiling:         512 MB (hard OOM kill above)
```
→ ~170 MB of headroom is all we ever have; a second page, a bigger prompt
pipeline, or any helper process can tip it over.

### OPTIONS ALREADY EVALUATED (do not re-suggest without new information)
- **Lightpanda browser** (Zig, 85 MB): loads Flow fine but the composer
  never renders (0 input fields after 50 s) → cannot generate. Dead end
  today.
- **Full Chromium**: 533 MB with Flow open → OOM. Dead on arrival.
- **Pure-HTTP engine** (no browser, ~60–90 MB): must forge Flow's internal
  RPC from Python; the `at`/same-origin coupling makes it fragile, and a
  non-browser TLS fingerprint is a classic reCAPTCHA tell. Untested, high
  risk.
- **Cloudflare WARP as egress**: WORKS as a fallback (userspace wireproxy
  SOCKS5 in-container), ~20 MB, +56 s per generation. But it is an IP fix,
  not a RAM fix.
- **Captcha-solver services**: irrelevant — invisible risk-score mode, no
  challenge exists to solve, tokens are context-bound (would look WORSE).
- **Spawn-per-job** (kill chrome between generations): idle 167→50 MB but
  +20–30 s cold boot per generation; peak during renders unchanged.

### QUESTIONS FOR YOU (ranked by value)
1. Can the ~340 MB render peak be reduced while keeping a real, hydrated,
   logged-in page? (Chrome flags? `--js-flags` caps? renderer process
   limits? site-isolation tricks? a smaller CDP-driven browser that CAN
   render Angular?) Concrete configs with expected savings.
2. Is a credible path to a browserless (<100 MB) Flow client possible?
   What exactly would it take to pass reCAPTCHA Enterprise from a plain
   HTTP client — and is the `at`/same-origin coupling of Flow's RPC
   documented anywhere? Any known implementations?
3. Architectures to serve 2–4 parallel generations at $0 (multiple free
   accounts/regions, queue fan-out, provider mix) without tripping the
   (IP+account) reputation scoring?
4. Anything smarter than keepalive+cooldown for surviving reCAPTCHA IP
   flags on a shared datacentre IP that cannot be changed on demand?

### CONTEXT YOU CAN ASSUME
- Browser fingerprint spoofing (UA, brands, webdriver, stealth init
  scripts) is already in place and previously fixed this exact error.
- The service already serializes generation (one at a time) — volume
  bursts are what historically triggered the flag.
- Free-tier platform constraints are fixed (no disk, 0.1 CPU, no TUN).
