# VERDICT.md — the HTTP-mint proposal, verified against sources

*Every factual claim in the researched plan was checked against the actual
repos before building. This is what verification found, what it changes, and
the build order that follows from it.*

---

## 1. The repos are real, licensed, and match their descriptions

| claim | verification |
|---|---|
| BypassV3 (EvickaStudio) exists, Apache-2.0, single-file `bypass.py`, `requests` only | ✅ confirmed — 370-line bypass.py, LICENSE = Apache 2.0, pyproject only |
| supports reCAPTCHA **Enterprise** (`/enterprise/anchor` → `/enterprise/reload`, protobuf) | ✅ confirmed in README + code (`enterprise=True`, `anchor_url_for_site_key`) |
| README is honest about scores ("does **not** guarantee a higher score") | ✅ confirmed — the exact honesty the plan cited |
| flow2api (TheSmallHanCat) exists, active (pushed 2026-11-08), pins `curl-cffi>=0.13,<0.14` | ✅ confirmed in requirements.txt |
| flow-google-api-kit | ⚠️ exists but the named repo didn't resolve — treat as secondary reference only |

## 2. THE BIG ONE — flow2api does NOT self-mint over HTTP. At all.

The plan's central proof was "flow2api proves daily in production that
plain-HTTP firing works". Verification of `src/services/flow_client.py`
(5,833 lines) says otherwise:

- **Zero** occurrences of `enterprise/anchor`, `enterprise/reload`, or any
  self-minting — it never performs BypassV3's exchange itself (grep count: 0).
- Its recaptcha tokens come from exactly three places, all external:
  1. **YesCaptcha / CapSolver** — PAID third-party solver services
     (`_resolve_third_party_captcha_method`, `yescaptcha_api_key`,
     `capsolver_api_key`, minScore 0.7/0.9 task types);
  2. **a browser captcha service** (`BrowserCaptchaService`) — a real
     browser mints the token;
  3. **a "personal" browser** (the user's own logged-in browser profile).
- When the recaptcha evaluation fails it retries via *more* browser captcha
  (`browser_captcha_generation_retries`).

**Consequence for us:** the production proof is real but it is a proof that
the FIRE layer (batchexecute with cookies + at + token) works over plain
HTTP **given a token from somewhere**. It is NOT proof that self-minting
passes Flow's hardened key. The plan's own §5 honesty ("BypassV3's 0.9
scores are on demo/test keys; hardened keys can score HTTP-minted tokens
~0.1 — that's why §8 and §7 are mandatory") stands, and is now *reinforced*
by flow2api's behavior: the busiest production deployment of this idea chose
paid solvers + browser minting over HTTP self-minting.

## 3. What survives verification — and it's a lot

The architecture's spine is sound and every load-bearing piece checks out:

- **Fire layer (curl_cffi + cookies + at + token → batchexecute)**: real,
  production-proven by flow2api's 5.8k-line client. The pinned
  `curl-cffi>=0.13,<0.14` matches. The `)]}'` / `wrb.fr` parsing model is
  exactly what flow2api does.
- **BypassV3 as the mint**: real code, correct endpoints, single dependency.
  Its `session` is a plain `requests.Session` — the three patches the plan
  wants (attach cookies, proxies, force www.google.com) are all one-line
  additions (it already defaults to www.google.com; only cookies + proxies
  need adding).
- **The fallback ladder**: sound. And we already possess rung 3 — our
  existing Chrome engine, which is *also* the fallback's mint (its in-page
  `grecaptcha.enterprise.execute()` with action `GENERATE`, sitekey
  already in our env: `6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV`).
- **Two-429 discrimination, AIMD pacing, no-probe-during-cooldown**: matches
  our own live observations (retry storms dig the hole deeper).
- **The economic math**: happy path ~80 MB / $0; worst case is today's
  architecture as insurance. Downside bounded, upside large.

## 4. The one number nobody can promise

Whether BypassV3's HTTP-minted token scores >0.something on Flow's
**hardened** key from our egress IP with our cookies attached. flow2api's
choice of paid solvers is weak evidence it does NOT (or did not when they
built it). Our cookies + WARP egress are the two levers the plan adds over
a bare BypassV3. **This is not researchable — it is only testable.** Hence
the validation gate is the first thing we build, not the last.

## 5. BUILD ORDER (what "apply it" means here)

Phase 0 — the gate decides everything (~1 session):
  1. Vendor `bypass.py` (Apache-2.0; keep license header) + patch:
     `cookies=` and `proxies=` params on the session.
  2. Add `curl-cffi` dep; write the fire layer against our captured RPC
     shape (we already have ogiZ0b request[4]/[5] mapped from earlier HAR).
  3. Wire the A/B harness: cell A = current browser path (control),
     B/C/D = HTTP mint × {direct, WARP} × {cookies on mint, not}.
     10 generations per cell, count UNUSUAL_ACTIVITY + media per cell.

Phase 1 — only if B/C/D pass ≥90%: switch the default path, demote Chrome
  to the ladder's rung 3 (cold mint-only fallback), keep everything else.

Phase 1′ — if HTTP mint fails: keep today's engine as primary (unchanged,
  nothing breaks), and evaluate the *only* other proven-at-scale token
  source the research surfaced: browser-mint + HTTP-fire (~200 MB, still
  deletes the 340 MB UI-driving peak, still faster boots). That is a real
  improvement even without BypassV3.

## 6. Cost of being wrong, either way

- Gate says HTTP works: RAM 167→~80 MB idle, peak 340→~90 MB, and the
  render peak (the OOM risk) leaves the happy path forever.
- Gate says HTTP fails: we lost one session of testing, keep the current
  engine, and gain a browser-mint+HTTP-fire option that still cuts the peak.

Both outcomes are strictly better than today. That asymmetry is why this is
worth building.
