# Public Job Search — Design Brief

**Status:** Exploration / pre-design (decisions captured, not yet scoped for build).
**Date:** 2026-07-24
**Authors:** Daniel + Claude (design conversation)

> This document captures a design conversation. It records the vision, the
> decisions made so far, what existing machinery we can lean on, the blind
> spots we pressure-tested, and the questions still open before a V1 build spec.

---

## 1. Goal & positioning

A **job-search-only** surface — search/browse job listings directly, _without_
the full profile → target → matched-ranking pipeline. Two intents, combined:

- **Growth funnel** — pull people in with free search; convert with "sign up to
  see how _you_ match + auto-tailor a résumé."
- **Activation on-ramp** — let people judge corpus quality for themselves and
  poke around before committing to the résumé-upload / profile flow.

**Not a replacement** for the matched experience. It sits _beside_ it and funnels
_toward_ it.

**The flywheel (the point, not a side-effect):** search demand → seed roles →
poll boards → corpus grows → better for everyone (esp. logged-in users). A
genuine compounding loop / network effect, not just a funnel.

---

## 2. What already exists we can leverage (~80% of the seed engine)

The "search a role → query boards → stash as a seed" loop is largely **existing
wyrdfold machinery re-triggered by a new signal (search demand)** instead of a
pasted URL:

- **from-url (#447/#448)** — input → detect ATS board → register pollable
  `source` → derive role/target → materialize + score jobs.
- **Discovery / lateral-discovery / suggest** + the **discovery cadence (#60-C)**
  — finds boards/roles on a recurring schedule (this is what "cadence searches
  find more" refers to).
- **`register_source_from_url` RPC + `source_ownerships` cap**, **`ats_detect.is_ats_url`**,
  **`job_ingest.materialize_and_score_job`**.
- **`normalize_label` / `derive_profile_from_label`**, **`title_triage`**, and
  **`role_family`** (the #278 off-family work) — role/title normalization for
  ranking + seeding, **model-free at query time** (`role_family` is precomputed).
- **Shared-target dedup** (`normalized_label` UNIQUE), **`job_embeddings` / HNSW**
  (available for a later re-ranking enhancement), **entitlements/credits**,
  **signup-mode + GoTrue perimeter** (open-signup mechanism already exists),
  **`job_list_cache`** (caching pattern).

**Novel build = (a)** a public, un-personalized search surface over the shared
`jobs` corpus, and **(b)** a throttled bridge from search demand into the
seed/discovery systems. Most logged-in "power actions" are **re-exposing existing
flows** from a new entry point.

---

## 3. Decisions locked in this conversation

| #   | Topic               | Decision                                                                                                                                                                                                                                                                                                                                                                      |
| --- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **Ranking**         | Title-similarity, synonym-tolerant (developer ≈ engineer ≈ dev; frontend ≈ front-end ≈ FE), reject off-role (backend ≠ frontend). Leverage keywords. **Model-free**; reuse `normalize_label` / `title_triage` / `role_family`. Embedding re-rank = possible later, logged-in-only enhancement. Accept V1 limitation: obliquely-named titles may be missed.                    |
| 2   | **Signup coupling** | **MAJOR TODO / prerequisite: open the app for public signups** (else the funnel dead-ends at a waitlist). Mechanism exists (deployment-modes / signup-mode / perimeter); flipping is a go/no-go + abuse-readiness (CAPTCHA) decision. Run beta testers in the meantime.                                                                                                       |
| 3   | **Abuse**           | **High priority.** Public users are model-free by construction (#4), so the surfaces are: corpus **scraping/enumeration**, **query-log** abuse, and **open-signup** abuse. Needs rate-limiting, bot detection, result-depth caps.                                                                                                                                             |
| 4   | **LLM boundary**    | LLM features are **logged-in only** and **credit-gated** (out of credits → top up to use a feature). **Public users never touch LLM _or_ embeddings** → public search is fully model-free.                                                                                                                                                                                    |
| —   | **Empty-search**    | **Honest coverage + async seed.** Seed on-demand **only for paying users**: a small, bounded immediate query for instant partial results, then the discovery **cadence** expands coverage over time. Guardrails: dedup seeds (skip already-covered roles/boards), hard-cap the immediate query.                                                                               |
| —   | **SEO**             | **Deferred.** Listing-level SEO is a trap (duplicate content vs. source ATS, freshness churn, thin-page domain-rep risk, redistribution exposure). If/when corpus is deep + fresh: **quality-gated role/aggregate landing pages** (unique aggregation + "see your match" CTA), not per-listing pages. `noindex` until then. Moat = matching/tailoring, not being a job board. |

---

## 4. Surfaces

**Public (logged-out):**

- Un-scored, **model-free** search over the shared, **live** jobs corpus.
- Title-first ranking (keyword + `role_family`), plus filters (location / remote /
  recency — TBD).
- Result row exposure: **OPEN QUESTION** (snippet + source link vs. full JD; depth cap).
- Strong "sign up to see how _you_ match + tailor a résumé" CTA.

**Logged-in:** (a **new tab**, kept **visually/structurally distinct from the
LLM-derived matched results** — manual keyword search ≠ your AI-matched ranking;
no match scores on the search surface, so search never gets mistaken for the
quality of the matching engine, and "see your match" stays the signup hook.)

- Same search **+ power actions** (re-using existing flows):
  - **Create target from a listing** — this is the from-url derivation path:
    **LLM-bearing, credit-gated, subject to the active-target cap.** _Not_ free /
    not available when out of credits.
  - **Add listing to an existing target** — cheap.
  - **Add-to-target feedback** — cheap.
- Doubles as the **out-of-credits manual fallback** (search itself needs no credits).

---

## 5. The seed flywheel (V2)

Paying-user search of a novel role → normalize to a role (`normalize_label` /
`title_triage`) → dedup vs. already-covered → **small bounded immediate poll** +
**register the role/source into the discovery cadence** → corpus thickens over time.

- **The hard link:** free-text → role → _which boards to poll_. from-url is easy
  (board is given); turning arbitrary role intent into the right boards is the
  **discovery problem** — historically the flakiest area. Success of the loop
  hinges on this step.
- **Shared-vs-private seed: DECIDED — seeds enrich the SHARED corpus; all users
  benefit.** Framed as a feature, not a leak. The seed **trigger** stays gated
  (paying users for the on-demand immediate poll; the cadence extends coverage
  to everyone) for abuse control (#3). Poisoning guardrails still apply because
  seeds are shared: `is_ats_url` gating, dedup, and validation of what gets
  polled. _(Knob for later: broadening the trigger beyond paying users trades
  abuse-surface for growth — keep gated unless #3 is well in hand.)_

---

## 6. Blind spots & mitigations (pressure-test)

| Blind spot                                                                                                           | Status / mitigation                                                                           |
| -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Ranking with no profile could surface un-ranked mediocrity, making the corpus look _worse_ than the matched product  | **Addressed** — title/keyword ranking (#1)                                                    |
| Public funnel dead-ends at the invite/waitlist perimeter                                                             | **Addressed** — open-signup TODO (#2)                                                         |
| Public search exposes the curated corpus to scrapers/competitors (the moat's raw material)                           | **Open / abuse #3** — depth caps, rate-limit, bot detection, snippet-not-dump                 |
| Cannibalization: the on-ramp becomes an off-ramp from profile-building                                               | Keep "see your match" hooks pulling toward matching; measure conversion                       |
| Not all power actions are free                                                                                       | **Addressed** — LLM=logged-in + credit-gated (#4)                                             |
| Searchable universe ≠ "all jobs" (US-only, off-family/liveness filters, stale/expired postings, near-dups)           | Live-only filter + result-level dedup + honest coverage                                       |
| Seeding the **shared** corpus reopens poisoning (SEC-H1 class); some jobs may have private (from-url) provenance     | **Open** — shared-vs-private seed decision; audit `jobs` provenance                           |
| Logging searches for demand = new PII / retention / consent + a scrape target                                        | Retention + consent design (honor Privacy policy); minimize stored query data                 |
| Public hot path on an IO-strained instance (57014 timeouts, embedding-write throttling, deferred P0 compute upgrade) | Search index (FTS/trigram) + caching + rate-limit; may couple to the compute-upgrade decision |
| Role → boards is the weak technical link                                                                             | Flagged; the discovery step needs to actually work for arbitrary role intent                  |
| No success metric / instrumentation                                                                                  | **Build into V1** — conversion, seed→growth, activation lift                                  |

---

## 7. Staging

**V1 — public read search + logged-in power actions (no seeding).**

- Indexed, cached, rate-limited search over the shared **live** corpus;
  title-first ranking; **model-free**; `noindex`.
- Logged-in power actions (reusing existing flows); out-of-credits fallback.
- **Instrumentation from day 1** (what people search, conversion, coverage gaps).
- Delivers _both_ goals at low backend risk and proves demand **before** wiring
  search to spend money polling.
- **Prereq:** open public signups for the funnel to complete (beta testers meanwhile).

**V2 — the paying-user demand → seed flywheel.**

- Throttled, deduped, small-immediate-query + cadence registration.
- Shared-vs-private seed decision resolved; poisoning guardrails.

**SEO — later, if warranted.** Quality-gated role/aggregate landing pages once a
role has deep, fresh corpus + a unique aggregate value-add. Not per-listing.

---

## 8. Resolved decisions (this pass)

1. **Public result exposure — DECIDED.** Logged-out row shows title + company +
   location + snippet + link to the **source** posting; full JD + "your match"
   gated behind login; hard result-depth cap (anti-scraping). "Good enough for
   now."
2. **Shared-vs-private seed — DECIDED.** Shared corpus; all users benefit
   (see §5). Trigger stays gated; poisoning guardrails apply.
3. **Placement / IA — DECIDED.** Public: dedicated **`/search`**. Logged-in:
   **new tab**, kept **distinct from the LLM-derived matched results** (manual
   keyword search ≠ AI-matched ranking; no scores on search).
4. **Success metric — DIRECTION.** Track **search usage/volume** and **signup
   lift** as headline signals; be **creative** about what else to gather
   (what's searched → coverage gaps → seed priorities; search → action
   conversion) and how it's used. Net view: positives outweigh negatives; build
   flexible instrumentation and iterate.

_Proposed defaults (react if you disagree):_ live-only filter; title-primary
search + location/remote/recency filters; small-seed = 1–2 boards / N listings;
a demand/dedup threshold for seeding.

---

## 9. Dependencies / major TODOs

- **[MAJOR] Open public signups** — prerequisite for the funnel. Mechanism exists;
  needs the go/no-go + abuse readiness (CAPTCHA). Beta testers meanwhile.
- **[HIGH] Abuse-prevention workstream** — public-search rate-limit / bot
  detection / enumeration caps; open-signup abuse; query-log hygiene.

---

## 10. V1 build plan (implementation)

**Grounded against the codebase (2026-07-24).** The decisions above (§7/§8) are
locked; this is _how_ V1 gets built. The code confirms the hard parts already
exist: `search_jobs` is fully un-personalized, the slowapi limiter already keys
per-IP for tokenless callers, and the **waitlist** (`app/routers/waitlist.py` +
`src/app/api/waitlist/route.ts`) is a proven unauth, BFF-secret-gated, per-IP
endpoint to clone. V1 is mostly wiring a second, locked-down front door to
existing machinery — plus the snippet and instrumentation.

### Linchpin — a separate public endpoint + an auth-adaptive `/search`

- **Don't** make the authed endpoint auth-optional: `verify_api_key_or_jwt` is a
  **router-level** dependency, so dropping it for one route drops it for the whole
  router. Add a **distinct** `GET /public/search` (`app/routers/public_search.py`)
  gated by `require_bff_secret` + a tighter per-IP `@limiter.limit`, calling the
  same `search_jobs`. The authed surface stays byte-for-byte unchanged.
- **Don't** host both `(app)/search` and `(public)/search` — same-URL collision.
  Relocate the one route to an **auth-adaptive** segment `src/app/search/` whose
  `layout.tsx` calls `getUser()` once and branches shell (app sidebar vs. public
  header + CTA); extract the shared shell into `AppShell`. Allowlist `/search` in
  `src/proxy.ts` exactly like the `/terms`·`/privacy` branch (targeted — `(app)/*`
  stays gated).

### Slices (PRs, security-core first)

1. **Public API endpoint** — `public_search.py`: `require_bff_secret` + per-IP
   limit (`10/minute;60/hour`) + **hard depth cap** (`page_size ≤ 20`,
   `offset ≤ 40`, vs. the authed `25`/`250`) → `search_jobs`; mount in `main.py`.
2. **Snippet projection** — add `snippet` to the model; a page-only
   `description_html` fetch → strip tags → truncate (~180 ch) for the ≤20 page
   rows (heavy column never read in bulk). Full JD never leaves the server.
3. **Public BFF route** — `src/app/api/public/search/route.ts` cloning the
   waitlist (no Bearer, inject BFF secret, forward trusted `x-real-ip` →
   `x-forwarded-for`).
4. **Routing + shell** — extract `AppShell`; relocate `/search` to the
   auth-adaptive segment; `proxy.ts` allowlist; `noindex` metadata.
5. **Logged-out rendering** — thread `isAuthenticated` into `JobSearchExplorer`:
   power actions only when authed; snippet rows + a "sign up to see how you match"
   CTA when logged-out; pick the public fetch URL.
6. **Instrumentation** — a `search_events` table (RLS deny-all, service-role
   writes, **no raw IP / no user_id**), written fire-and-forget/batched (mirror
   `cost_log_buffer.py`), with a retention purge — the volume / coverage-gap /
   conversion metrics.

### Cross-cutting risks / prereqs

- **[OWNER go/no-go] Open public signups** — the funnel dead-ends at `/login`
  until `signup_mode=open`; per §7 ship V1 behind the flag with beta testers and
  flip separately. Not a build blocker.
- **`WYRDFOLD_BFF_SECRET` is a hard launch gate** — `require_bff_secret` _fails
  open_ when unset, so it must be set on **both** Railway and Vercel before
  `/public/search` is reachable (fine for the low-stakes waitlist; not for a
  scrape-sensitive route).
- **Shared cache correctness** — the anonymous `job_list_cache` is shared across
  audiences ONLY while the projection is audience-identical; if a public-only
  field ever diverges, add an audience dimension to the cache key.
- **IO / snippet** — title search rides the existing `idx_job_postings_title_trgm`
  GIN index; no FTS needed for V1. The only new DB pressure is the snippet's
  page-bounded `description_html` read (cached); escalate to a denormalized
  `search_snippet` column only if slow-request logs show it.

_Critical files:_ `app/routers/waitlist.py` (clone target), `app/services/job_search.py`
(reuse), `app/rate_limit.py` + `app/dependencies.py::require_bff_secret` (posture),
`src/proxy.ts` (allowlist linchpin), `src/app/(app)/search/JobSearchExplorer.tsx`
(refactor), `src/app/api/waitlist/route.ts` (public BFF precedent).

---

## 11. Search UX redesign — card grid + contextual detail (PR5 line)

**Settled 2026-07-25, mockup-driven (iterated with Daniel).** Supersedes the row-list
result layout. Applies to **both** audiences; the differences live in the per-card footer
and the detail. The guiding prototype is the iterated card-grid + contextual-detail
mockup (browse-first, bind→unlock).

### 11.1 Card grid (replaces the row-list)

- **3-col responsive grid** (→ 2 tablet → 1 mobile).
- Card = company monogram (per-company hue; real logos via **#470**) · role title ·
  `Company · Location` · 2-line snippet · salary (or "Salary not listed") · posted.
- **The whole card is the click target → the listing detail.** No per-card external
  link and **no per-card conversion hook** — browse-first (the earlier per-card
  "See how you match" was removed as too aggressive; it fought the "let them poke
  around before committing" on-ramp).
- **Logged-in footer** (per card): a quick **Add to target** (free, feedback-like) —
  OR a **pipeline-state badge** `✓ In "<target>"` when the listing is already in one of
  the user's targets. _(Quick-add: always-visible in the mock; hover-reveal is a
  candidate to keep the resting grid calmer — TBD on feel.)_
- **Logged-out**: no footer; the card is purely a click-target.

### 11.2 The listing detail (modal with a real URL)

- Opens **over the grid** (keeps browse context). Target: a Next **intercepting route**
  so `/search/[id]` is shareable / deep-linkable and renders as a full page on a direct
  hit. **V1 ships a plain client modal; the URL/intercepting-route is a fast-follow.**
  **[SHIPPED — fast-follow landed]:** the detail is now URL-addressable — cards link to
  `/search/<id>`, a soft nav intercepts into the modal (`@modal/(.)[id]`), a hard load
  renders the standalone page (`GET /public/listings/{id}` behind the BFF; middleware
  allowlists UUID-shaped `/search/<id>` only; noindex per §10).
- **Header:** monogram · role title · `Company · Location`, then **"View original
  posting ↗" as a link on its own line** (both audiences). The full JD lives at the
  source — we preview, we don't republish.
- **Body:** chips (salary / posted / location) + the snippet.
- **Actions — contextual on target-membership** (§11.3).
- Dismiss: ✕, click-outside, Esc; focus returns to the card.

### 11.3 The target-bind model (the crux)

The LLM actions are **target-level, not listing-level** — you don't match or tailor a
bare listing, you match/tailor a _listing-in-a-target_. So the detail's actions are a
**sequence**, gated on whether the listing is bound:

- **Unbound listing → only the two BIND actions:**
  - **Add to a target** (primary, free) — files the listing against an existing target;
    **acts as positive feedback** (scores it + sharpens how that target scores). Same
    effect path as user feedback.
  - **Create a target from this role** — background **from-url** derivation; returns
    immediately, **toasts on completion**, the listing lands in the new target's
    listings. Uses AI credits.
  - One-liner: _"Add to a target to unlock LLM pipelines."_ **[COPY-TBD:** "LLM
    pipelines" reads as internal jargon to a job-seeker; candidate softer wording
    "unlock AI matching & tailoring".**]**
  - Match/tailor are **not shown** — nothing to score against yet.
- **Bound listing (already in a target) → the LLM actions UNLOCK:**
  - **See how you match** (primary) — score this role **against the target it's in**;
    returns fit + matched skills + gaps. (Answers "how will we know?" — we score against
    that target.) Uses credits.
  - **Tailor a résumé for "<target>"** — tailoring scoped to the target (avoids the
    drift a bare-listing tailor would cause). Uses credits.
  - **Add to another target** (free).
  - Shows the state `✓ In "<target>"`.

### 11.4 Credit rules

- **Browsing is always free** — search, the grid, the detail's info + source link. No
  credits, no gate, for everyone.
- **LLM actions are credit-gated** — match, tailor, create-target. Surface the cost + the
  balance.
- **Out of credits ≠ a wall** — a top-up nudge; search + add-to-target keep working (the
  out-of-credits manual fallback the epic wanted). Never block browsing.
- **Add-to-target is free** (feedback path, not LLM).

### 11.5 Funnel (logged-out)

Browse-first, no per-card pressure. Logged-out card = clean click-target → detail. The
detail carries the info + source link + a **soft, non-blocking allusion** ("there's more
when you're signed in — members see how they match + tailor a résumé; sign up free").
One calm conversion moment, in the detail, never on the grid. A quiet "Sign up" in the
header; no persistent banner.

### 11.6 Build slices

1. **Card grid** — replace `JobSearchExplorer`'s row-list with the 3-col card grid;
   logged-in footer (add-to-target + pipeline-state); wire add-to-target (exists).
2. **Contextual detail** — the listing detail (plain modal first; URL fast-follow);
   header source link; unbound (add/create-target — both exist) vs bound (match/tailor).
3. **Backend — match a search listing vs a target** — score a specific posting against
   one of the caller's targets; return fit / matched keywords / gaps. Reuses the scoring
   layer. LLM/embedding surface → grow the mock + regression tests.
4. **Backend — tailor for a listing-in-target** — tailoring scoped to the target. LLM
   surface → mock + tests.
5. **Wire** the frontend match/tailor actions to (3) + (4).
6. Public routing + logged-out rendering + instrumentation (§10's PR3–6) fold in behind.

### 11.7 Open / TBD

- Copy: "unlock LLM pipelines" wording (§11.3).
- Detail as modal-with-URL (intercepting route) vs plain modal — start plain, add URL.
- Quick-add always-visible vs hover-reveal — decide on feel.
- Whether match/tailor need dedicated per-listing endpoints or can reuse the
  target-scoring / tailoring paths with a listing argument — scope in slices 3–4.
