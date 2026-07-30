# Decisions & war stories (append-only)

The incidents behind the standing rules. Newest first. Each entry: what
happened, what we decided, where the rule lives now.

## 2026-07-29 — Authed BFF silently dropped a filter param (twice)

`/search` has TWO BFF routes (authed proxy `/api/jobs/search`, public
forwarder `/api/public/search`). A filter param added to the API + component
reached one route but not the other — the missing side returned unfiltered
results under an active-looking filter UI. Shipped twice (location/recency in
the #467 fast-follow; `salary_floor` at the salary-filter launch, caught by
the release gate's authed browser drive). **Decision:** one shared
`SEARCH_FILTER_PARAMS` list (`apps/wyrdfold/src/lib/api/searchFilterParams.ts`)
feeds both routes + a spec pins that the authed proxy forwards every entry
(#531). Adding a search filter = one entry there.

## 2026-07-29 — "The poller will heal it" is conditional, not guaranteed

Stored-data defects (escaped HTML, null salaries) were assumed to self-heal as
boards re-poll. False: the conflict-update refreshes a row only when its source
polls AND its title prematches **today's** active-target keywords AND it passes
the US gate AND the board still lists it. A corpus accumulated under
since-deactivated targets never converges. **Decision:** stored-data defects
get one-shot idempotent backfill scripts (`apps/wyrdfold-api/scripts/`); never
rely on poll-cycle convergence for rows outside the current target set (#527).

## 2026-07-06 → 2026-07-29 — Unbounded poll batches starve the fleet

A backlog cycle tried every due source at once, blew the 1200s watchdog, the
abort left the tail un-stamped, and the next cycle repeated the identical
oversized batch — 1,110/3,231 sources ended >2× overdue. **Decision:**
`poll_max_sources_per_cycle` (250, most-overdue-first) + the Phase-1
negative-verdict cache for rejected titles (#526).

## 2026-07-24 — Frontend/API deploy skew (#459)

A release changed an API contract; Railway auto-deployed the API but Vercel is
not git-connected, so the OLD frontend ran against the NEW API and broke live.
**Decision:** `vercel --prod` is a standing release step — frontend and API
deploy together. Lives in the `/release` skill, step 5.

## 2026-07-22 — Disabled legacy anon key 500-stormed `GET /jobs`

A release flipped `GET /jobs` onto the RLS user client whose prod
`SUPABASE_ANON_KEY` was a disabled legacy key — invisible to every local/CI
check (all used a working local key). **Decision:** the API boot-guards its
Supabase keys (`_probe_supabase_keys`), and every release ends with a prod
smoke on the changed surface because prod config is only proven by touching
prod. Lives in the `/release` skill, step 6.

## 2026-07-1x — RLS policies never reached prod

Three releases shipped RLS-dependent code whose policies existed only in
`supabase/migrations/` — nothing applies migrations to prod automatically, so
status writes broke live. **Decision:** a release containing migrations is not
done until they are applied AND verified against prod; migrations-first is the
safe order (they're written backward-compatible). Lives in
`.claude/rules/api-validation.md` + the `/release` skill, step 5.

## 2026-07-15 — The 73 ms keepalive misdiagnosis (retracted)

`/jobs` cold-start latency was blamed on httpx keepalive tuning; measurement
proved the real cause was QUERY read-amplification (18,900 → 117 pages via the
cross-target RPC redesign, #365/#377). The keepalive claim was retracted.
**Decision:** prove the diagnosis by measuring before proposing a fix — "likely
X" is a lead, not a conclusion. (The universal rule lives in claude-rules; this
is the repo's canonical example.)
