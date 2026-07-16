# Performance Audit — wyrdfold (2026-07-16)

Scope: SQL + Python (API) + TypeScript (Next.js FE). Method: two specialist finders
(Python/SQL query-shape + async-correctness; FE/TS render + fetch + bundle) + Claude's
verification of each acted-on finding against code. Known-open #365 (cross-target
`status=new` timeout) and tonight's release fixes were excluded from re-reporting.

**Headline:** The fundamentals are strong. **Async correctness is airtight** (AST-verified
two ways + the #107 CI guard — zero unwrapped blocking calls in any `async def`), **DB
batching is disciplined** (consistent documented `.in_()` chunking, no N+1 in the hot
poller/scoring/notify paths), and the **FE is disciplined client-side** (abort/request-id
guards, chained-timeout polling with caps + visibility parking, dynamic imports for heavy
deps, no prefetch storm — that theory is **disproven**: Links stop at `loading.tsx`
boundaries). The real wins cluster on **server-render data-seeding (FE)** and **a
#365-class query + a missing cache (BE)**.

---

## HIGH

### PERF-1 — Home page couples every widget to the #365 query, and the proxy retry doubles the load

FE. `dashboard/page.tsx:74-90` (`fetchTodayInitial` `Promise.all`), no `<Suspense>` in the route.

Five upstream reads are awaited together and packed into one prop, so the entire "Today"
content blocks on the slowest/most-fragile — which includes the cross-target
`status=new` list (**#365, 8-10s/500 under load**) and `pipeline-counts` (~1.6s). Under the
#365 spike the app's default landing page hangs 8-10s (or renders zeros after a 500) even
though the counters/profile-gate/targets returned in a few hundred ms.

**Amplifier (verified):** `fetchJsonFromWyrdfoldAPI` retries 5xx once (`proxy.ts:67`
`_DEFAULT_RETRIES=1`), so every Home load on the #365 500-path **re-issues the cross-target
query a second time** — more latency _and_ more load on the collapsing endpoint.

- **Fix:** split `fetchTodayInitial` into independent async sub-components each behind its
  own `<Suspense>` (gate + counter strip vs Top Matches), so the fast parts paint at
  ~1.6s and Top Matches streams / shows its own state without holding the page hostage;
  and pass `retries: 0` to the Top Matches call specifically (`:76`). De-amplifies #365.
- **Effort:** medium (~1-2h). The `retries:0` change alone is trivial and high-value.

---

## MEDIUM

### PERF-2 — Insights endpoints have no result cache (the historically 9-41s screen)

BE. `routers/insights.py:101-147` → `services/insights.py`.

`/insights/pipeline`, `/insights/targets`, `/insights/skills-cost` recompute their GROUP BY
aggregations on **every** request (pipeline alone = 2 RPCs + a status-log window read + a
documents read), with **no cache** — unlike `/jobs` and `/jobs/pipeline-counts` (both under
the 60s `job_list_cache`). The insights dashboard toggles period (7d/30d/90d/all) across
three tabs, so one visit re-runs the heaviest aggregations several times. This is the
endpoint memory flags as historically **9-41s**.

- **Fix:** wrap each handler's return in a per-`(user_id, period)` `TTLCache` (reuse the
  existing `TTLCache` + `make_cache_key`); invalidate on the same events as `job_list_cache`.
  The RPC-first aggregation is already well-built — this is purely the missing cache.
- **Effort:** trivial-moderate, low risk. **Highest-value cheap BE win.**

### PERF-3 — Per-target list path is a #365-class fetch-all-then-paginate

BE. `routers/jobs.py:937-945` (`_list_jobs_for_target_two_query`).

Fetches **every** candidate `scores` row for the target (no `.limit()`/`.range()`), then
ranks + paginates in Python — the identical anti-pattern to #365, bounded to one target.
Same 57014-timeout risk on any `?target_id=` view forced onto the two-query path (axis
weights, per-target preferences, logistics filter, `status=archived`, or RPC unavailable).
**Shared root cause with #365:** the display score (axis-weighted blend + read-time recency
decay) and the Pending-below-graded bucket are computed in Python, so the keyset RPC can't
express the ORDER BY. Partially mitigated by tonight's `jobs!inner` live-join (shrinks the
candidate set to live rows).

- **Fix:** solve once for **both** routes — push `display_score` + `is_pending` into the DB
  (generated column or an RPC that emits them), then keyset-paginate server-side.
- **Effort:** needs-migration; co-design with **#365**. Track together.

### PERF-4 — BFF proxy re-parses + re-serializes every JSON response

FE. `lib/api/proxy.ts:234-239` (+ multipart `:339`). Hot caller: `/api/jobs` list.

Success path does `text() → JSON.parse → NextResponse.json` (re-stringify) for an identical
passthrough — full parse **and** re-serialize, payload held as both string and object, on
every proxied read/write (concentrated on the `/api/jobs` list every filter/sort/load-more).
Not per-request latency, but wasted Vercel compute + memory on the busiest path.

- **Fix:** on 2xx + `application/json`, `new NextResponse(rawBody, {status, headers:{content-type}})`;
  keep `JSON.parse` only on the error/non-JSON branch (which already 502s via `nonJsonUpstream`).
  Validate the non-JSON path + status passthrough with the proxy tests.
- **Effort:** small (~30min).

### PERF-5 — `/jobs/{id}` detail is a fully client-rendered 2-level fetch waterfall

FE. `jobs/[id]/page.tsx:20` → `JobDetailPage.tsx:50-77` (job fetch on mount) → `:82-102`
(second `/targets/mine` fetch) → downstream history/analysis/resume/cover-letter.

The detail page renders nothing data-bearing on the server; the critical path is
TTFB(shell) → hydrate → fetch job → render panel → fetch history/analysis/resume. The
list/dashboard already server-seed; the detail page should too (fetch posting + first active
target in the server `page.tsx`, thread as props). Keep only the LLM analysis client-side.
(Same fetch-on-mount pattern, lower urgency: `/profile`, `/settings` — but those parallelize.)

- **Effort:** medium (~1-2h).

---

## LOW / quick wins

- **PERF-6** BE. Redundant `user_targets` read on the default `/jobs` view — `get_user_target_ids`
  (`jobs.py:1555`) **and** `list_user_targets` (`:1640`) read the same rows; the latter already
  returns `target_id`. Derive `user_target_ids` from one `list_user_targets`. Trivial;
  cache-miss only.
- **PERF-7** FE. `dashboard/loading.tsx:17-21` skeleton says "Dashboard"/"Your job search at a
  glance"; page renders "Home" → visible title-swap flash. Match the copy. Trivial (~2min).
- **PERF-8** BE. `services/lifecycle.py:89-114` `_deactivate_idle_targets` N+1 write loop —
  one UPDATE + one label-read per idle user. Single `UPDATE … WHERE user_id IN (…) RETURNING`.
  Background job (no user latency), but a launch cohort aging out bursts serial writes at the
  small prod instance. Moderate.
- **PERF-9** BE. `services/insights.py:441,469` `compute_pipeline` runs current + prior-window
  RPCs sequentially — fold into PERF-2's cache work (extend the RPC to return both windows).

## Watch-items (not findings)

- **Index confirmation (→ DBA):** the #365/PERF-3 shape wants a composite `scores(target_id,
excluded)` covering `(job_posting_id, score, scoring_status)` + a partial index on live
  `jobs`; the per-chunk overlay wants `user_jobs(user_id, job_posting_id)`.
- **`job_list_cache max_size=128`** (`cache.py:88`): per-`(user, filter-combo)` keys can thrash
  past 128 as the beta opens, collapsing the hit rate and re-exposing the slow paths. Bump + monitor.
- **Single-process amplifier:** 1 worker / 1 replica shares the ~40-thread anyio pool; a 4-8s
  `/jobs` (#365) holds a thread for its full duration — enough concurrent slow list calls
  saturate the pool and stall fast requests. Another reason to land #365/PERF-3.
- **Doc drift:** `supabase_pool.py:31` says the async client is "not wired into any call path
  yet" — it now backs the scheduler sweeps + `poll_db_*` seam. Update.

## Reviewed and found HEALTHY

Async correctness (zero blocking in `async def`, AST-verified + #107 guard); DB batching
(all bulk reads/writes chunked ~150-200 for the Kong 8KB cap; no N+1 in poller/scoring/
notify); `/jobs/pipeline-counts` (cached + RPC-first); scoring/triage/fit compute (zero DB
calls — pure compute); connection pooling (sync H1.1 shared pool, async H2 bounded);
scheduled sweeps (`.range()`-paginated, `scheduler_runs`-ledgered). FE: prefetch fan-out
disproven; recharts dynamic + not in Home bundle; abort/request-id guards on all list hooks;
tiptap/dompurify lazy; `next/image` + system fonts (no web-font CLS); ≤20-row pages (no
virtualization needed).

---

## Actionables (priority)

| ID     | Sev  | Layer | Action                                              | Effort                  |
| ------ | ---- | ----- | --------------------------------------------------- | ----------------------- |
| PERF-1 | HIGH | FE    | Suspense-split Home + `retries:0` on Top Matches    | med (retries:0 trivial) |
| PERF-2 | MED  | BE    | Insights TTLCache                                   | trivial-mod             |
| PERF-3 | MED  | BE    | per-target list DB-side pagination (co-design #365) | needs-migration         |
| PERF-4 | MED  | FE    | proxy raw-text passthrough on 2xx JSON              | small                   |
| PERF-5 | MED  | FE    | server-seed `/jobs/{id}` detail                     | med                     |
| PERF-6 | LOW  | BE    | dedupe `user_targets` read on `/jobs`               | trivial                 |
| PERF-7 | LOW  | FE    | fix dashboard skeleton title                        | trivial                 |
| PERF-8 | LOW  | BE    | batch `_deactivate_idle_targets` UPDATE             | mod                     |
| PERF-9 | LOW  | BE    | one-call current+prior insights RPC                 | small                   |

**Overnight self-implement plan:** the trivial/small verified wins — PERF-2 (insights cache),
PERF-4 (proxy passthrough), PERF-7 (skeleton), PERF-1's `retries:0`, PERF-6 (dedupe read) —
as small PRs to develop (self-merge on green, no release). PERF-1 Suspense split, PERF-3/#365,
PERF-5 detail-seed are medium/migration — documented for the owner.
