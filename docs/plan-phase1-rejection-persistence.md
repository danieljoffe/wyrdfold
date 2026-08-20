# Plan: persist Phase-1 rejections (kill the daily re-bill)

Status: **implemented & validated** (branch `feat/phase1-rejection-persistence`);
awaiting PR review → release. Prod migration NOT yet applied.
Diagnosed: 2026-08-12, from prod `llm_costs` + `sources` + `jobs`. Author: Claude + Daniel.

Validation (2026-08-12): 3,064 unit tests green (9 new store-contract tests +
4 rewritten poller-path tests with precondition asserts); sabotage-checked —
silently-dropped writes fail 3 tests, weakened profile_version keying fails 2
(the fail-open read path masks key sabotage unless tests carry a positive
control; they now do); 238 integration tests green against the local stack,
including a real-PostgREST roundtrip (upsert conflict, TTL cutoff, FK CASCADE
on target delete); migration applies cleanly in the full chain
(`supabase db reset`); ruff + mypy clean.

## Problem

Phase-1 title triage (`relevance.title_triage`) is **63% of all LLM spend**
($231.48 of $366.04 logged since 2026-05-30). The bulk of it is not grading new
postings — it is **re-saying "no" to the same titles roughly once a day,
forever**.

A rejected title never ingests (no `jobs` row), so it re-enters the triage
candidate set on every poll of its source. The negative-verdict cache built for
this (#514, `poller.py` `_PHASE1_REJECTIONS`) cannot hold the memory:

1. **24h TTL by design** — even a perfectly-working cache re-bills the entire
   standing rejected corpus every 24 hours. Rejected postings stay open for
   months; the corpus never shrinks.
2. **In-process dict, wiped by every deploy** — single uvicorn worker
   (Dockerfile CMD, no `--workers`), so the dict _does_ work between restarts,
   but Railway auto-deploys `main` near-daily. At our release cadence the two
   causes are redundant: fixing either alone changes nothing.

## Measured evidence (2026-08-12, prod)

All queries in the appendix; re-run them post-release to verify the win.

- Last 7d: **416,689 verdicts / 93,370 calls / $14.87**, flat ~59.5k
  verdicts/day. Flat daily volume is corpus-shaped, not news-shaped.
- **(source, target) pairs paying on ≥5 of 7 days carry 76.5% of verdict
  volume** (≥4 days: 87%). A standing re-bill signature — fresh postings can't
  produce near-identical counts day after day.
- **PwC micro-case**: 3 Workday boards (distinct tokens, `job_count` 200 each,
  240-min interval). **87 new jobs in 7d**, yet ~**5,900 verdicts** paid across
  the 5 active targets (~150/target/day, flat).
- **Jobgether**: polled once daily (1440-min); pays its ~108-verdict standing
  set on _every_ poll. The daily re-bill in its purest form.
- **Fresh-work bound**: 17,305 `scores.promising=true` pairs created in the
  window. At a 25–40% fresh-admit rate (the prompt is deliberately generous),
  legitimate fresh work is ~43–69k verdicts/wk. Observed 417k →
  **waste ≈ 75–90% of Phase-1 ≈ ~50% of the whole LLM bill**.
- Prod config at diagnosis: `PHASE1_TRIAGE_MODEL=deepseek-v3-2`;
  `PHASE1_REJECTION_TTL_HOURS` unset → default 24.0.

### Evidence caveats (don't re-litigate these)

- `llm_costs.metadata->>'source'` is the **company name**, not the board token
  — PwC's 3 boards are indistinguishable there, so intra-day multi-payment for
  one company is _not_ by itself proof of a cache wipe. The days-paying
  distribution is the clean fleet-wide evidence.
- Per-call cost split on deepseek-v3-2: output ≈ 44%, fresh input ≈ 30%, cache
  reads ≈ 26% (avg 179 in / 1,563 cache-read / 172 out per call). The prompt
  preamble is already well cached; small batches are a minor cost, not the
  dominant one.
- **A stricter Phase-1 gate makes this bill BIGGER, not smaller** — every extra
  rejection joins the standing corpus and re-bills daily. Cost-motivated gate
  tightening is exactly backwards until rejections are persistent.
- On-demand grading doesn't touch this line item either: triage runs per
  source-poll, pre-ingest, blind to user activity.

## Fix

Replace the in-process dict with a Postgres table. Memory of a rejection must
survive restarts and outlive 24 hours.

### Table (additive migration)

```sql
create table public.phase1_rejections (
  target_id       uuid not null references public.targets(id) on delete cascade,
  profile_version integer not null,
  title_norm      text not null,
  confidence      integer,          -- verdict confidence, observability only
  model           text,             -- model that judged, observability only
  judged_at       timestamptz not null default now(),
  primary key (target_id, profile_version, title_norm)
);
alter table public.phase1_rejections enable row level security;
-- no policies: service-role only, like llm_costs
```

- **Key semantics identical to the dict**: `(target_id, profile_version,
title_norm)` with `title_norm = " ".join(title.lower().split())`. A profile
  edit bumps `profile_version` → every cached rejection misses → full re-judge
  under the new profile. Unchanged from today.
- `model` is _not_ in the key: a model/prompt change does not auto-invalidate.
  Operational note: after a material prompt/model change, manually
  `delete from phase1_rejections;` (one statement, corpus re-warms in a day).
- Only **rejections** are stored (parity with #514): admits ingest, and
  known-external-id checks already stop their re-triage. Low-confidence admits
  gated out by `admitted()` stay uncached — re-judging borderline titles is the
  cheap side of that trade (existing comment in poller stands).

### Code

New module `app/services/relevance/rejection_store.py`:

- `async fetch_rejected_titles(supabase, target, titles) -> set[str]` — one
  chunked `.in_()` query per (target, candidate-batch); chunk ≤150 keys (URL
  414 limit, see #57 notes). Read-side TTL filter:
  `judged_at > now() - phase1_rejection_ttl_hours`. **On any error: log
  warning, return empty set** — a cache failure means paying the LLM again,
  never a broken cycle.
- `async record_rejections(supabase, target, rejections) -> None` — one
  batched upsert per (target, source-batch), `on_conflict` refreshes
  `judged_at`/`confidence`. On error: log, continue.

Wiring — both consult sites keep their exact loop shape, the membership test
just moves from dict to prefetched set:

- Scheduled path (`poller.py` ~1680): prefetch before the candidate loop;
  collect `not verdict.promising` titles; one `record_rejections` after the
  batch loop.
- On-activation/bootstrap path (`poller.py` ~3080): same.
- **Delete** `_PHASE1_REJECTIONS`, `_PHASE1_REJECTIONS_CAP`,
  `_phase1_rejection_key`, `_phase1_cached_rejection`,
  `_phase1_record_rejection`, and rewrite the block comment at
  `poller.py:413`. One source of truth; the "in-process only" justification
  (advisory lock) is obsolete.
- Per-cycle diagnostics: aggregate hits/misses across the cycle, one log line —
  `phase1 rejection store: N hits (LLM calls avoided), M misses` — so the
  store's effect is observable in Railway logs (unlike the dict, whose
  invisibility is how this went unmeasured).

### Config

- `phase1_rejection_ttl_hours` default **24.0 → 1440.0** (60 days). The `0 =
disabled` contract is preserved (store becomes a no-op read/write).
- No new feature flag: TTL=0 is the kill switch, and the fail-open-to-LLM error
  path means the blast radius of a store bug is cost, not correctness.

### Retention

Add `phase1_rejections` / `judged_at` to `retention.py::purge_expired_records`,
horizon = TTL + slack (90 days). Steady-state size ≈ standing rejected corpus
(~60–100k skinny rows) — trivial.

## Tests (validate-before-PR)

- Store unit tests: title normalization parity with the old dict key; chunking
  at 150; TTL cutoff; profile_version mismatch = miss; fetch error → empty set
  - warning (assert the LLM then gets called — the fail-open must be
    _observable_, not assumed); record error → no raise.
- Poller tests (rewrite the existing `_PHASE1_REJECTIONS` tests in
  `tests/test_poller.py` + `conftest.py`): cycle 1 rejects a title → row
  persisted; **fresh store/"restarted process" in cycle 2 → LLM mock receives
  zero calls for that title** (assert the precondition — the row exists —
  before asserting the skip); profile bump → LLM called again; TTL=0 → store
  inert, every title sent.
- Sabotage checks per the mock discipline: poison the store with a
  wrong-profile_version row and prove it does NOT suppress the LLM call.
- `uv run pytest -q`, `ruff check`, `mypy app`, and `-m integration` against
  the local stack before push.

## Deploy

1. Migration is **additive** → apply to prod + verify + stamp **before** the
   merge (repo migration discipline; additive-before-merge ordering rule).
   Code ordering is safe either way — a missing table reads as a store error →
   fail-open to LLM.
2. PR → `develop`, then release via `/release` (Railway auto-deploys API from
   `main`; no FE change, Vercel untouched, Railway SKIPPED-for-FE caveat n/a —
   this IS an API change).
3. The corpus warms over ~1 day (each standing title pays once more), then
   volume collapses to fresh-only.

## Success metrics (re-run ~3 days post-release)

- `relevance.title_triage` verdicts/day: **~59.5k → expect <15k** (fresh-only;
  exact floor depends on true fresh-title supply, which this fix will reveal).
- Days-paying distribution: ≥5-of-7-day pairs should fall from **76.5%** of
  volume toward a churn-only residue.
- Triage $/day: ~$2.1 → expect ~$0.3–0.5. Total LLM bill roughly halved.
- Railway log line shows hits ≫ misses after day 1, and hit counts _persist
  across a deploy_ — the original failure mode, now directly observable.

## Explicitly out of scope (parked, with reasons)

1. **Verdict output slimming** (drop/shrink the `title_prefix` echo, ~44% of
   per-call cost is output): it's the #47 id-transposition guard, and any
   prompt/schema edit trips `test_prompt_regression` → spend-bearing evals +
   golden re-baseline. Owner call, separate PR.
2. **On-demand / search-driven grading redesign**: decide on product merits
   against the post-fix cost baseline, not against a bill that was ~50-90%
   artifact. Background _catalog polling_ is nearly free (HTTP, not LLM);
   what could go lazy is per-target grading — but weigh losing the
   "agent that notifies you" property first.
3. **$366 logged vs ~$600 OpenRouter spend reconcile**: `llm_costs` starts
   2026-05-30 and may not capture everything (pre-log spend, failed-call
   retries, provider fees). Check `/api/v1/key` `limit_remaining` runway
   (NOT `/credits` — key cap binds first; see cost memory).
4. **Cross-company title memory** (same title rejected under many boards):
   the key is title-only per target, so this already cross-covers — no extra
   work needed. Noted so nobody "adds" it later.

## Appendix — diagnosis queries (prod, read-only)

Spend by purpose:

```sql
select purpose, count(*) calls, round(sum(cost_usd)::numeric,2) usd,
       round(100.0*sum(cost_usd)/sum(sum(cost_usd)) over (),1) pct
from llm_costs group by purpose order by usd desc nulls last;
```

Standing re-bill signature (the headline number):

```sql
with per_pair as (
  select metadata->>'source' src, metadata->>'target_id' tgt,
         count(distinct created_at::date) days_paying,
         sum((metadata->>'batch_size')::int) verdicts
  from llm_costs
  where purpose='relevance.title_triage' and created_at > now() - interval '7 days'
  group by 1,2)
select days_paying, count(*) pairs, sum(verdicts) verdicts,
       round(100.0*sum(verdicts)/sum(sum(verdicts)) over (),1) pct
from per_pair group by 1 order by 1;
```

Verdict volume vs fresh supply, per company:

```sql
-- verdicts: group llm_costs by metadata->>'source'
-- fresh:    select company_name, count(*) from jobs
--           where cataloged_at > now() - interval '7 days' group by 1
```
