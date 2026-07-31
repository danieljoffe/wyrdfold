# Decisions & war stories (append-only)

The incidents behind the standing rules. Newest first. Each entry: what
happened, what we decided, where the rule lives now.

## 2026-07-30 — The prescan gate is retired by its own shadow data; embeddings survive on ordering

The #60/#89/#90 cosine pre-gate (cut Phase-2 LLM grading spend by admitting
only jobs whose embedding matches the target) ran in shadow for months. The
schema audit forced the verdict days before the shadow corpus hit its own
30-day retention drain: joining 2,477 graded pairs against shadow cosines,
the SIGNAL is real (promising jobs avg cosine 0.38 vs 0.25 for duds; 83% of
gate-passers are promising) but the armed threshold (0.3981, calibrated
2026-07-04) would have dropped **61.5% of promising matches**. Meanwhile the
cost problem it was built for had shrunk under it (deepseek + caching + caps →
grading ≈ $1/mo). A first read of the raw admit-rate matrix was mistaken for a
"signal is worthless" verdict — the ~5% admit rate was the _designed_
asymmetry; only the outcome-join could judge it. **Decision:** retire the gate
(drop `prescan_cosine_threshold`, gate/holdout/allowlist code + flags;
`prescan_shadow` + recorder dropped after its retention drain ~2026-08-09) but
KEEP the full embedding stack — live grades show cosine is the strongest cheap
fit predictor (avg fit ~7→67 monotone), so it orders the Phase-2 daily-cap
queue; the threshold trade-off curve is preserved in the audit doc for any
future revival (0.26–0.28 ≈ keep ~90% of promising, halve grade spend).
Follow-on ops: stage-2 (#544) NULLed all target embeddings, leaving that
ordering blind — `backfill_target_embeddings.py` re-run approved. Lessons: an
experiment's exhaust needs a decision deadline (retention nearly deleted the
answer unread), and judge a gate by outcomes joined, not admit rates. Full
analysis: `.claude/docs/audit-wyrdfold-schema-debt-2026-07-30.md` (Group E).

## 2026-07-30 — Schema-audit Group D: names must tell the truth (+ paused targets leave the list)

The audit's "confusing but live" group was resolved in one pass. **Decisions:**
(1) `user_profiles`/`user_targets` `job_score_threshold`/`sms_score_threshold`
→ rename to `email_alert_threshold`/`sms_alert_threshold` (they gate alerts,
never the list; the UI already says so — only the DB names lied; bundle with
the release that arms alerts). (2) `source_ownerships` →
`source_registrations` (it's a from-url registration/abuse-cap ledger; payer
logic never reads it). (3) `/jobs` switches from all-linked-targets to
ACTIVE-memberships-only — the old behavior ("paused targets stay browsable")
had no remembered rationale; pausing a target now removes its jobs from the
list. (4) `activation_status` keeps its home on `targets` (derivation is
target-global state) but gets one converged terminal state, an error-retry
path, and a stuck-state sweep (a prod row sat at 'polling' for 16 days).
(5) The `scores` state trio (`scoring_status`/`promising`/`is_graded`) is
overlapping-but-load-bearing — left alone until a Phase-2 refactor. Details:
`.claude/docs/audit-wyrdfold-schema-debt-2026-07-30.md` (Group D). Not yet
built.

## 2026-07-30 — The provider posted-date was captured all along; a misnomer hid it

The product wanted to weigh new vs old listings by the ATS's own posting date.
The schema audit found that data was being captured the whole time: every
adapter maps its best posted/created field (Lever `createdAt`, Ashby
`publishedAt`, SmartRecruiters `releasedDate`, Workday `postedOn`, JSON-LD
`datePosted`, Greenhouse `updated_at`) into one column — named
**`greenhouse_updated_at`** — populated on ~98% of rows and read by nothing.
The misleading name meant recency weighting (#460-464) was built on
`first_seen_at` (our observation time, which overstates freshness for boards
discovered late), and `first_seen_at` itself is a byte-identical duplicate of
`created_at` (both DEFAULT now(), never app-written, no divergence path).
**Decision:** exactly two timestamps — `cataloged_at` (rename of
`jobs.created_at`; `first_seen_at` dropped, aliased in RPC returns + the
scores denorm) and `source_posted_at` (rename of `greenhouse_updated_at`);
recency weighting + FE "Posted" read `coalesce(source_posted_at,
cataloged_at)`; the manual/from-url path writes NULL instead of `now()`;
`StandardJob.updated_at` renamed `posted_at`. Lesson: a column named after one
provider's field will be treated as that provider's noise — name columns for
their semantics, not their first source. Full analysis:
`.claude/docs/audit-wyrdfold-schema-debt-2026-07-30.md` (Groups B/C). Not yet
built; rides with the Group A cleanup release.

## 2026-07-30 — `targets.is_active` double duty vs the app-owned catalog (decided, not yet built)

The schema audit found `targets.is_active` doing double duty: a trigger
(`trg_sync_target_active`) maintains it as a cache of "has an active member,"
while the #543 catalog seeds it manually as "instance sponsors this target."
The trigger only knows the first rule, so the catalog-search _happy path_ —
a user follows a catalog target (inactive link, `from_input.py`) — fires the
trigger and permanently deactivates that catalog target's ingestion; an
activate→deactivate cycle does the same. The seed script's own docstring
documents the reliance ("the trigger never touches them"); nothing enforces it
at runtime. **Decision (option A — re-semantics):** drop the trigger; demote
`targets.is_active` (rename to `app_active`) to carry ONLY the manual floor,
default false, never written by user actions; the pipeline derives
active-for-ingestion as `app_active OR EXISTS(active membership)` at read time
in `crud.get_active`. Rationale: the derived half of the column is redundant
and is exactly the part that breaks; the manual half is irreducible. Removes
the last user-derived state from `targets`, completing the user-agnostic
split. Full analysis:
`.claude/docs/audit-wyrdfold-schema-debt-2026-07-30.md` (P0 section).

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
