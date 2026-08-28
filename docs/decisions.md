# Decisions & war stories (append-only)

The incidents behind the standing rules. Newest first. Each entry: what
happened, what we decided, where the rule lives now.

## 2026-08-28 — The board already knew the country (and a bulk upsert is not a per-row upsert)

The 2026-08-27 correction below left a hole: lazily tagged rows never reach
`QUALIFICATION_ARCHIVE_NON_US`, so 13.4% of new intake — 133 of 134 of it non-US
— now stays publicly visible until something grades it. The obvious fix, running
the deterministic `is_us_location()` parser at ingest and persisting its verdict,
is worth **nothing**, and the reason is worth remembering: the poller already
runs that parser as an L1 gate, and it drops a listing only when the location
carries a non-US hint AND no US marker. Every row it ADMITS is, by construction,
one the same parser cannot call non-US. A gate and a classifier built from one
predicate cannot disagree. **Decision:** use a different, stronger fact — the
country Ashby / Lever / SmartRecruiters publish as a structured field, which
`board_metadata` was already normalizing to ISO alpha-2 and then discarding.
Measured on 23,908 live postings across 35 real boards: 27.0% of the postings the
L1 gate admits carry a board-stated non-US country. Against rows we hold and had
already tagged, the board and the model agreed 257/267 on non-US and 1,283/1,285
on US.

**A key present on ANY row of a PostgREST bulk upsert is written to EVERY row.**
The rows that omitted it get NULL. #846's entire design — "omit the key and the
stored column is untouched" — is true only when _no_ row in the batch supplies
it, which is not the case for a heterogeneous board batch. Proved on the local
stack, not assumed: two rows, one carrying `is_remote`, the other not; the second
row's stored `is_remote` came back NULL, in both input orders. That is why the
board's `is_us` verdict is a targeted post-upsert UPDATE and not an upsert key,
and it means `is_remote` / `employment_type` have a live blanking bug of their own
(filed separately). `country` was safe to add to `board_columns` only because
every poller payload already carries that key unconditionally.

**No migration.** `jobs.country` holds a display vocabulary (`US`, `UK` — never
`GB`; #805 was a filter sending alpha-2 at it and matching nothing), so the
board's ISO code is TRANSLATED on the way in through a map composed from
`location_parse`'s own token table — a country appears there only when both
modules already know it, so the two spellings cannot drift. A code with no
display spelling (`CH`) writes nothing to the column but still produces a
verdict: not being able to spell Switzerland says nothing about whether the role
is in the United States. The parser and the board agreed 15,439 times and
disagreed 22, and the board was right in every sampled disagreement — "CA -
Toronto" parses as California, "IN - Bangalore" as Indiana, "London, ON" as the
UK.

**A value that was inert became load-bearing, and nobody re-checked its
validation.** `normalize_country` accepted any two-letter alphabetic string as a
country. That was harmless while the result was discarded; the moment it drove a
one-way archive, `TX` / `NY` / `FL` became "not the United States" and would have
pruned US roles. `location_parse` had already written the warning down — "bare ISO
codes need comma context — too collision-prone", "two-letter abbreviations must
arrive UPPERCASE to count as a state" — and this module did the exact thing that
comment refuses. **Ask what a value's validation was written for before you give
it a new job.** Two guards now: a real ISO 3166-1 register (which alone kills
`TX`, `NY`, `WA`, `OH` — they are not countries anywhere), and, for the ~25 codes
that genuinely are both a country and a USPS state (`CA`, `DE`, `IN`, `MD`, `MT`
…), a requirement that the location's own parse not read as US before the row may
be pruned. The blunt fix — refuse every colliding code — was measured and
rejected: those codes carry 883 of the 4,285 prunes, a fifth of the whole
feature, and across 21,891 live postings with a board country not one was a US
state (`CA`→"Ontario - Remote", `MD`→"Chișinău", `PA`→"Panama City, Panama"). The
corroboration guard withholds exactly one posting. Separately, US territories
have their own ISO codes: `PR`, `GU`, `VI`, `AS`, `MP`, `UM` were being archived
as foreign, and a live Lever posting located "American Samoa" was in the sample.

**One predicate, both writes.** The country column and the US verdict were
initially allowed to disagree — a "New York, NY; London" posting with a `GB`
postal address got no verdict (correctly vetoed) but still got filed under `UK`.
That is the worst of both: a wrong /jobs facet AND a disabled `country = 'US'`
veto in the tagger, on exactly the multi-country class the veto exists for. Any
reason to distrust the board's country enough to withhold a verdict is a reason
not to file the posting under it.

**Scope, stated honestly.** Greenhouse and Workday publish no country in their
cheap path, and they are 60% of the live corpus, so this removes roughly 38% of
the non-US pollution — the share attributable to boards that answer the question.
Within that share it is near-complete (93.9% of admitted postings carry a
country). Rows skipped as byte-identical (#642) are deliberately not revisited:
back-filling history is the per-cycle full rewrite that change removed.

## 2026-08-26 — Work bought at ingest, read at grade time (qualification tags go lazy)

The qualification tagger ran on every newly-upserted or content-changed job at
ingest. Nothing at ingest reads its output: the only consumers are the Phase-2
runner's US and family gates and its ordering, and grading is bounded by a
per-target daily cap, so most of what ingest classified was never looked at.
**Decision:** tag a listing when something is about to read the tags, exactly
as job embeddings went lazy in the 2026-07-30 Disk IO slim-down — `a job is
materialized exactly when first needed`. The tagger core moved to
`services/qualification/materialize.py` (`ensure_job_tags`); the poller calls
nothing at ingest.

**CORRECTION (2026-08-27), and the important part of this entry.** The release
claimed public `/search` was unaffected, on the grounds that `_SEARCH_COLS`
selects no tag column and the one tag predicate, `is_us IS NOT FALSE`, admits
`NULL`. That is true of the _query_ and false of the _population_, which is what
matters. Tagging at ingest is what produced `is_us = false` and, with
`QUALIFICATION_ARCHIVE_NON_US` on in prod (a `False` default, overridden),
stamped `archived_at` on high-confidence non-US rows before they were ever
served; `qualification_archive_non_genuine` did the same for talent-pool
non-postings. Lazily tagged rows reach neither rule, and `NULL` sails through
every read gate. Measured over the 7 days before the switch: **13.4% of tagged
rows were archived by the tagger's own rules at tag time** (133 of 134 non-US).
That share of new intake now stays publicly visible until something grades it.
The deterministic `is_us_location()` L1 gate still drops the obvious cases at
ingest, so the exposure is the ambiguous residue — but it is a real exchange of
catalog precision for lower ingest cost, and the release described it as no
change at all. Validating query shape is not validating the visible corpus.

Four things this taught, each now pinned by a test:

**Placement inside the consumer is a spend decision.** The Phase-2 runner's
`is_us` and family gates run _before_ its ordering and daily-cap trim. Calling
the tagger there would tag the whole candidate set to grade at most the quota —
the same "buy for everything, read a few" shape we were removing, one layer
down. It runs _after_ the trim, and the two gates are then re-applied to the
freshly tagged rows. A row the post-trim gate rejects is not backfilled from the
next candidate: refilling means tagging a second tranche, which re-opens the
hole.

**A sweep that consumed its own output stops advancing when you remove the
output.** `_backfill_qualify_stale` selected `role_family IS NULL`
oldest-first each cycle; the tagging it did is what made the set shrink. With
tagging removed, "untagged" is the _normal_ state of the catalog, so it would
have (a) re-bought the entire catalog's tags a batch at a time had the tagging
call been left in, and (b) re-checked the same oldest batch forever once it was
taken out. Kept the free liveness half, gave it a rotating cursor — and then had
to fix that cursor, because the first version walked by OFFSET. An offset over
this predicate silently SKIPS rows: the set is unstable (a row leaves it the
moment the sweep archives it), so surviving rows shift backward underneath a
cursor that only moves forward. It walks by keyset on `(cataloged_at, id)` now.
The lesson is narrower than "use keyset": the instability was _introduced by the
same change_ — before this, the sweep's own tagging is what removed rows, and
nobody had asked what happens to a paginated walk when the thing removing rows
becomes something else.

**Moving a reader onto a new call path revives every partial `SELECT` upstream
of it.** Two grade-path callers built job dicts from narrow projections. Missing
columns did not fail — they degraded silently: the content hash computed over
different inputs can never match the stored one (re-tagged every pass), and a
missing `is_remote`/`employment_type` made #846's defer-to-the-board rule read
"the board said nothing" and write the inference over the employer's own answer
(#795's contradictions, re-introduced). The columns the tagger reads are now a
declared contract, `materialize.TAG_INPUT_COLUMNS`, asserted against each
caller's projection.

## 2026-08-18 — reading config is not tracing the path (#841 was backwards)

#841 was filed claiming free accounts drain the operator's LLM credit with no
aggregate ceiling. The opposite is true: free accounts are **refused** with a
402 before any LLM client is constructed. The filing traced `resolve_llm_quota`
correctly — and that trace was downstream of a gate free users never reach.
`llm/__init__.py:224` raises on an **OR**: `BYOK_REQUIRE_USER_KEYS` _or_ the
caller's plan being BYOK, which the saas free tier always is. The flag was
indeed unset; the plan branch fires on its own. **Decision:** a claim about what
code _costs_ or _permits_ requires walking the call path from the entry point,
not reading the settings that appear in it. Config tells you what a branch would
do if reached; only the path tells you whether it is reached. Two aggravating
details worth remembering: the issue's own "Verify before flipping" section
prescribed the check (create one free account) that would have caught it, written
by the same author who then skipped it — and the test suite was **already
correct**, with `test_get_client_saas_free_without_key_is_refused` setting the
flag to `False` explicitly. A coverage gap was assumed and asserted before the
tests were read. Rule lives in the general "prove the diagnosis" rule; the
misleading log that pointed at the wrong variable was fixed in #859.

## 2026-08-18 — a server route handler cannot read a URL fragment (#856)

Beta invites bounced to `/login?auth_error=missing_code` while the owner's own
sign-in worked perfectly through the same callback. The invite was landing with
a valid session in the **URL fragment** (`#access_token=…`), which browsers
strip before the request leaves — so the server-side handler saw no `code` and
no `token_hash` and fell through. The cause was upstream: `invite_user_by_email`
is **server-initiated**, so no PKCE `code_verifier` exists in the recipient's
browser, and a `{{ .ConfirmationURL }}` template therefore falls back to implicit
flow. Sign-in is **browser**-initiated, gets `?code=`, and works. **Decision:**
whether an auth callback receives a query or a fragment is decided by _who
initiated the flow_, not by the URL — reason about the initiator first. Fix is
the `token_hash` template form, which bypasses GoTrue's redirect entirely and is
device-independent (it also survives opening the mail on another device, which
PKCE does not). Two second-order lessons: the first hypothesis (redirect
allowlist) was wrong and was only settled by asking for the **actual landing
URL**; and `GET /auth/callback 307` is logged identically for success and
failure, so the logs could not discriminate — the operator's answer could.
Standing risk: these templates live only in the hosted Supabase dashboard, so no
test, CI job or review can detect a regression (#860).

## 2026-08-18 — an absent log line is evidence (#862)

A Stripe webhook returned 200 and `user_profiles.plan` demonstrably flipped to
`starter`, but `_set_plan`'s `billing: plan=… user=…` line was nowhere in the
Railway logs. Chasing that discrepancy rather than accepting the happy outcome
found that **production discards every `logger.info`** — 84 call sites, including
all of `scheduler.py`'s outcome reporting. `init_logging` returns early unless
`log_format == "json"`, and the `root.setLevel(logging.INFO)` that keeps child
loggers off the stdlib WARNING default sits _inside_ that branch; `LOG_FORMAT` is
unset on Railway, so it never runs. uvicorn's own access logs kept appearing,
which made the gap look like normal traffic. **Decision:** log **level** and log
**format** are independent concerns and must not share a branch. Also: when a
verified outcome is missing its expected log line, treat the absence as a finding
rather than noise — the outcome being correct is exactly what makes the missing
line easy to wave away. Aggravating detail: #859 had just improved one of those
`logger.info` lines, validated against tests rather than against production log
output, so the improvement is invisible in prod until this is fixed.

## 2026-08-18 — one environment named `development` was serving production (#861)

`STRIPE_SECRET_KEY` on the service behind wyrdfold.com is an `sk_test_` key, so
no real customer could subscribe — a live card against a test-mode Checkout is
rejected, and Stripe renders a customer-facing "Sandbox" badge on the payment
page. Combined with #841 (free accounts walled), there was **no path by which
anyone could become a paying user**. The root cause is topology, not a pasted
value: there is one Railway project with one environment, named `development`,
and it is production. The test key is correct _for a development environment_;
there is simply nowhere else for it to live. **Decision:** fix the environment
split, not the key — a wrong value replaced in place leaves the same trap for the
next environment-specific setting, and #856/#860 are the same shape (hosted
config with no non-production place to exercise it). Silver lining used
deliberately: because prod was in test mode, the full checkout → webhook →
`user_profiles.plan` chain was exercised end to end at zero cost, so when live
keys land the only untested variable is the key itself.

## 2026-08-18 — `promising` now means "admitted", not "Phase 1 admitted"

`scores.promising` arrived as the Phase-1 title-triage verdict and its column
comment said exactly that. But it is also what `target-membership` reads to
decide pipeline membership — the "✓ In &lt;target&gt;" badge, and the gate on
match/tailor. So when `add-to-target` scored a pair without setting it, an
explicit user add produced a row the badge logic excluded **by construction**:
the panel flipped to a bound state and a reload erased it (#830). **Decision:**
`add-to-target` sets `promising = True`, widening the column from "Phase 1
admitted this" to "something admitted this", with the user as the other
admitting authority. Rationale: the column's JOB is to gate membership, and a
user asking for a job to be in a target is a stronger signal than a title-only
LLM guess — so the widened reading is the truthful one, not a convenience. The
alternative, a provenance column, is a migration and buys nothing the user's
action needs. Kept in its own greppable helper (`_user_admit_score_async`)
rather than folded into the force-include RPC, so the widening is findable.
Known limit, accepted: membership also applies the #277 family gate, so an
OFF-family user add still won't badge — bypassing that _does_ need provenance,
and neither case in #830 was off-family. Lesson: when one column serves two
readers, the name should describe what it gates, not who first wrote it.

## 2026-08-18 — A measurement is a claim: three predicates that each invented a bug

Three separate times in one day a prod query produced an alarming number, and
all three were the query's fault, not the code's. (1) Ingestion "fell 93%"
after the catalog-grading change — the windows were US-evening vs US-overnight;
the same UTC slice two days earlier, **before** the release, was identical
(weekends run ~4/h against a weekday ~47/h). (2) Salary parsing had "133
failures" — the test was `salary_min IS NULL`, but `parse_salary_text`
documents `"up to $X"` as a **MAX-only** bound, so correctly-parsed rows have a
NULL min; the real corpus-wide failure count is **1**, a typo in an employer's
own posting. (3) Newly-graded scores "still carried logistics" — counted by
`updated_at`, which includes OLD rows re-graded; Phase 2 no longer _emits_
logistics so the upsert doesn't touch that column, and doesn't blank it. On
`created_at` the count is 0. **Decision:** before reporting a number, state the
predicate and check it against the code's documented behaviour — `created_at`
answers "did the new config take", `updated_at` only "was this re-graded"; and
never compare a time window against a differently-shaped one. Acting on any of
the three would have reverted a working change or "fixed" a correct parser.

## 2026-08-18 — Two LLM writers were guessing at facts the boards publish outright

Phase 2 inferred `remote_status`/`country`/`salary` into
`scores.logistics_filters`, and the qualification tagger inferred
`jobs.is_remote`/`employment_type` — while Ashby publishes `isRemote`,
`workplaceType`, `employmentType` and a structured `postalAddress`, Lever
publishes `workplaceType` and an already-ISO `country`, and SmartRecruiters
models `location.remote` + `location.hybrid` as separate booleans. The fetchers
read ~6 of 24–40 published fields and dropped the rest. Two inference paths
over the same question also disagree — #795 measured 229 prod contradictions on
remote alone. Worse, the tagger runs on the **upsert result** and overwrote
those columns unconditionally, so reading the board (#847/#848) was a complete
no-op until the tagger learned to defer (#851): the board fact was written and
overwritten inside one poll. **Decision:** structured field where the board
publishes it; deterministic parse of the location string for Greenhouse and
Workday, which publish no remote flag but state it in the text; LLM never.
Lesson: when a value has more than one writer, find the LAST one before
claiming the first works. Also: Workday's list entry has six fields and we
already read all six — its country sits behind the per-posting detail call
#828 cut to ~3%, so "does the platform publish it" is the wrong question;
"which endpoint, at what cost" is the right one.

## 2026-08-18 — A workaround outlived the problem it was working around

Catalog targets (`app_active`, no active `user_targets` link) were Phase-1
graded every cycle on the instance key, producing scores nobody read — the
dominant LLM line item while zero user targets were active. The reason it was
there: a 2026-07-30 rule ("never spend money nobody will consume") had starved
the public /search corpus to one sponsored target's family, and the fix was to
let catalog targets grade again. But that starvation came from dropping them
out of the ACTIVE SET, which stopped their **source polling** — the corpus
starved for lack of ingestion, not grading. Meanwhile `/search` was changed to
read `jobs` directly, skipping `scores` entirely, which fixed the real cause
and made the workaround redundant. Nobody removed it. Verified before shipping:
28.3% of recently-ingested jobs carry no promising score at all, so ingestion
does not depend on admission. **Decision:** gate grading only, never polling,
behind `GRADE_CATALOG_TARGETS` (default off). Lesson: one flag (`app_active`)
drove two unrelated concerns — "poll this target's sources" and "grade against
it" — so fixing one necessarily moved the other.

## 2026-08-18 — Config outranks code, so a shipped fix can do nothing

The landing page probed `/signup-mode` without the BFF secret, got a 403, and
its fail-safe reported `closed` — so the homepage signup CTA could never open,
whatever the operator switch said. Invisible by construction: a 403 from your
own perimeter is a misconfiguration, but the code treated it identically to
"backend down". Found only by reading HTTP logs and noticing the same endpoint
returning 200 from one caller and 403 from another. The same shape nearly
repeated within the day: `LOGISTICS_EXTRACTION_ENABLED=true` on Railway would
have overridden a new `False` default and left half of #851 inert — caught
because the deploy note said so, and the var was removed **before** the merge.
**Decision:** when a change's effect depends on an env var, say so in the PR
and verify the var's state as part of the release, not after. Lesson: a
fail-safe that swallows a misconfiguration converts a loud bug into a silent
one; distinguish "degraded" from "wired wrong".

## 2026-07-31 — The URL-health net ran daily and could never catch anything

The dead-link archival cascade (HEAD checks → 3 consecutive failures →
archive) was correctly built, scheduler-armed, and ticking every day — and had
archived exactly **0 jobs ever**. Cause: `due_url_health_jobs` ordered
`last_url_check_at ASC NULLS FIRST`, so a row that just took strike 1 went to
the back of the entire never-checked backlog; at batch 50/day the second
strike arrived ~82 days later while 30-day retention archived everything
first. Silence looked identical to health. **Decision ("fix the net"):**
strike-carrying rows are served FIRST (a dying URL confirms on consecutive
ticks → archives in ~3 days), then never-checked, then stalest; batch default
50 → 250. Lesson: a safety net needs a liveness proof — if it has never
fired, verify it _can_; ordering choices compose into starvation at scale.
(Same audit batch: `jobs.department` + `jobs.us_confidence` dropped —
verified reader-free.) Full analysis:
`.claude/docs/audit-wyrdfold-schema-debt-2026-07-30.md`.

## 2026-07-30 — The prescan gate is retired by its own shadow data; embeddings survive on ordering

The #60/#89/#90 cosine pre-gate (cut Phase-2 LLM grading spend by admitting
only jobs whose embedding matches the target) ran in shadow for months. The
schema audit forced the verdict days before the shadow corpus hit its own
30-day retention drain: joining 2,477 graded pairs against shadow cosines,
the SIGNAL is real (promising jobs avg cosine 0.38 vs 0.25 for duds; 83% of
gate-passers are promising) but the armed threshold (0.3981, calibrated
2026-07-04) would have dropped **61.5% of promising matches**. Meanwhile the
cost problem it was built for had shrunk under it (deepseek + caching + caps
had made grading cheap enough that the gate no longer paid for itself). A first
read of the raw admit-rate matrix was mistaken for a
"signal is worthless" verdict — the ~5% admit rate was the _designed_
asymmetry; only the outcome-join could judge it. **Decision:** retire the gate
(drop `prescan_cosine_threshold`, gate/holdout/allowlist code + flags;
`prescan_shadow` + recorder dropped after its retention drain — the recorder in
R2, the table itself in R3 §1 on 2026-08-11 once prod confirmed it had drained
to 0 rows, `20260811000000_r3_drop_prescan_shadow.sql`) but
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
