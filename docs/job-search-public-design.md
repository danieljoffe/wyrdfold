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
