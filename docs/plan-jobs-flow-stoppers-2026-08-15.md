# Implementation plan — /jobs flow stoppers (2026-08-15)

Addresses every finding in `docs/ux-sweep-jobs-2026-08-15.md`. Owner decisions taken
before work started:

| Decision                   | Choice                                                                            |
| -------------------------- | --------------------------------------------------------------------------------- |
| P1-3 delete semantics      | **"Remove"**, and actually remove the listing **from the target**                 |
| P1-5 cover letter          | **Warning + "write it anyway" override** (accepts prompt edit + eval re-baseline) |
| Release shape              | **One release**: both P0s, all P1s, safe P2s                                      |
| If prod verification fails | **Fix forward**                                                                   |

Note on the two conflicting inputs: the release-shape option said "defer anything needing
a prompt/eval or a schema change", but the cover-letter answer explicitly bought the
override. The explicit choice wins — the override ships, with `eval_cover_letter.py` run
and goldens re-baselined. The same reasoning admits the one migration that "Remove"
needs, since Remove is the P1 the owner personally redefined.

---

## A. Design decision that constrains PR-3 (read first)

The owner asked for Remove to "actually remove the job listing". A **global hard delete
is not available to us**, and this is not a preference — `DELETE /jobs/{id}`'s docstring
records that hard-deleting the shared `jobs` row destroys `status_log` / `user_jobs` for
every other user following that posting. That cross-tenant destruction was closed out by
a prior security audit (#29 round 3 / H1) and must not be reintroduced.

`scores.excluded` is also unusable as the removal flag, for two independent reasons:

1. **It is scorer-owned and recomputed.** `target_scoring.py` upserts
   `excluded=result.excluded` on every re-score, and its own docstring warns that the
   scorer's flag "overwrites prefilter exclusions on every re-score". A removal written
   there would silently come back on the next poll — the exact no-recourse trap.
2. **`scores` is per-target, not per-user.** Targets are shared with co-searchers, so
   writing removal there would remove the job from _other people's_ lists.

**Therefore:** removal is a new per-`(user, target, job)` fact in its own table that the
scorer never writes. This satisfies "remove it from the target" from the user's point of
view, durably, without cross-tenant damage.

---

## B. PR breakdown

Five PRs into `develop`, ordered so each is independently reviewable and revertible.
PR-1 and PR-2 are the P0s and go first.

### PR-1 — Add-by-URL: stop reporting success when nothing was added (P0-1, P1-4)

_Frontend only. No API change, no migration._

- `useAddJobByUrl.ts`: branch on the **body**, not just `res.ok`. `/jobs/manual` returns
  200 with `{success:false, needs_manual_fields:true}` when extraction fails.
  - `success === false` → error path with the real reason derived from `warnings`
    (`firecrawl_failed:http_403` → "This site blocks automated reading — paste the
    employer's ATS link instead"; `fetch_non_200` → "That URL didn't load").
  - Only toast "Job added" when `success === true && posting_id`.
- Replace `window.prompt` with a small in-app dialog (P2-13) so the URL can be validated
  inline and the failure reason rendered next to the input instead of vanishing into a
  toast. Reuses `ConfirmModal`'s primitives.
- When `needs_manual_fields` is true, offer the manual title/company/location fields the
  API is already asking for (`ManualJobResponse.extracted` pre-fills them) and re-POST.
- **P1-4**: mount the entry point from the list toolbar ("Add job by URL") so it is
  reachable at any list size, not only at 0–4 jobs. `JobsEmptyState` /
  `JobsThinResultsCallout` keep their buttons and call the same hook.

Tests: unit tests over the hook for `{ok:true, success:false}`, `{ok:true,
success:true}`, 400, and network throw. **The 200/`success:false` case must fail against
today's code** — that is the regression pin.

### PR-2 — Resume version restore (P0-2)

_API + frontend. No migration (`document_versions.payload_md` already exists)._

- `versions.py`: add `payload_md: str | None = None` to `ResumeVersion`. This is the
  whole root cause — `select("*")` already returns it and `extra: "ignore"` was eating it.
- `persistence.update_payload` (`persistence.py:284`): pass `payload_md` so snapshots on
  that path stop being markdown-less. Requires threading the markdown in from the caller.
- Frontend `ResumeReviewPage.tsx`: keep the "predates markdown" guard for genuinely old
  rows but **disable the Load button and explain it inline** rather than letting the user
  click into a dead-end toast.
- Confirm the restore path end to end: Load → ConfirmModal → `performRestore` writes the
  markdown back and snapshots the pre-restore state first.

Tests: API test asserting `payload_md` survives `list_for_resume` → `model_dump()` (fails
today); FE test that a version with markdown enables Load and one without disables it.

### PR-3 — "Remove from target" (P1-3)

_API + frontend + **one migration**._

Migration `supabase/migrations/<ts>_user_target_job_removals.sql`:

```sql
create table if not exists public.user_target_job_removals (
    user_id        uuid not null,
    target_id      uuid not null references public.targets(id) on delete cascade,
    job_posting_id uuid not null references public.jobs(id)    on delete cascade,
    removed_at     timestamptz not null default now(),
    primary key (user_id, target_id, job_posting_id)
);
create index if not exists idx_utjr_user_target
    on public.user_target_job_removals (user_id, target_id);
alter table public.user_target_job_removals enable row level security;
create policy "Users access their own removals" on public.user_target_job_removals
    for all to authenticated
    using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
grant select, insert, update, delete on public.user_target_job_removals to authenticated;
grant all on public.user_target_job_removals to service_role;
```

Purely additive — safe to apply **before** the merge.

- New endpoints: `POST /jobs/{id}/remove` (body: `target_id`, or all of the caller's
  active targets containing it when omitted) and `DELETE /jobs/{id}/remove` for undo.
- Read paths must exclude removals — **all four**, or the job reappears depending on
  sort:
  1. `get_target_jobs` (SQL) — anti-join on the new table
  2. `get_cross_target_jobs` (SQL) — same
  3. `_list_jobs_for_target_two_query` (Python)
  4. `_assemble_jobs_page` hydration (Python)
     Plus the target job **count** in `targets.py:473` so the badge matches the list.
- **Same migration fixes the archived leak** (see report): both RPCs currently apply no
  archived exclusion when `p_status IS NULL`. Add
  `AND ($4 IS NOT NULL OR COALESCE(uj.status,'new') <> 'archived')` so the RPC paths match
  the documented Python behaviour.
- Frontend: batch bar and row menu action renamed **"Remove"**; confirm copy becomes
  "Remove N jobs from {target}? They'll stop appearing in this target." — drop the false
  "can't be undone". Toast gets **Undo**.

Tests: an integration test that a removed job is absent from _every_ read path (both
sorts, both RPC and two-query), and that a re-score does **not** resurrect it — the
latter is the whole point of not using `scores.excluded`.

### PR-4 — Cover letter: pre-spend warning + "write it anyway" (P1-5)

_API (prompt) + frontend. Trips `test_prompt_regression`._

- Frontend: when the job's match analysis verdict is "Skip", both document buttons show a
  confirm naming the cost and the verdict, with two choices: _Cancel_ and _Write it
  anyway_. Non-Skip jobs are unaffected (no new friction on the happy path).
- `prompts.py` `COVER_LETTER_SYSTEM`: add a `STRETCH APPLICATION` block, active only when
  the caller passes the override, instructing the model to write the strongest honest
  letter from transferable skills and to **never decline to apply or editorialize about
  the mismatch**. Hallucination containment is untouched and still wins.
- Thread an `allow_stretch: bool` flag from the route to the prompt builder.

Required by CONTRIBUTING before merge:

1. Run `scripts/eval_cover_letter.py` (real spend, needs `OPENROUTER_API_KEY`), attach
   before/after to the PR.
2. `UPDATE_PROMPT_GOLDENS=1 uv run pytest tests/test_prompt_regression.py` so the golden
   lands as a reviewable diff.

New eval case: a deliberately poor-fit JD with the override on, asserting the letter does
not contain a refusal and invents no experience.

### PR-5 — Safe P2 polish

_Frontend only, no API, no migration._

- **#6** Row title → internal `/jobs/{id}`; a separate explicit external-apply icon keeps
  the ATS link (currently the only affordance, and it leaves the app).
- **#9** After "Re-adapt with AI", re-point the page at the new tailor record id instead
  of leaving the stale draft and an empty version panel on screen.
- **#10** Bulk status change in the batch bar (Saved / Not for me / Rejected).
- **#11** "Saved" indicator on the resume editor (autosave already works; it's invisible).
- **#12** Persist the ATS check result on the page instead of a toast that vanishes.
- **#14** Copy: "Review cover letter" to match "Review tailored resume"; job status
  reflects a cover letter existing.
- **#8** "Score 85+" — label the ungraded escape hatch in the chip so the result stops
  looking like a broken filter.

**Deliberately deferred** (recorded, not silently dropped): **#7** per-target tabs
showing unrelated ungraded intake. Correct fix is to gate ungraded jobs by the target's
family/keyword prefilter before they surface, which is scoring-pipeline work with its own
eval exposure — too big to ride along safely in an unattended release. Follow-up issue.

---

## C. Validation

Per repo rules, a PR ships already-proven.

- **Every PR**: `uv run pytest -q`, `ruff check`, `mypy app` for API; `nx test wyrdfold`,
  `nx lint wyrdfold` for FE. `ruff format` only on files actually touched.
- **Negative cases are the point** — each P0 gets a test that fails without the fix
  (200/`success:false`; `payload_md` stripped by the model).
- **Migration**: apply to local Supabase first, run `pytest -m integration`, verify RLS
  actually isolates by user before going near prod.
- **e2e**: grep `apps/wyrdfold-e2e` for the copy strings being changed ("Delete", "Job
  added") — e2e specs assert copy that jest does not.

## D. Release + deploy order

Via the `/release` skill.

1. Apply the additive migration to prod **before** the merge (safe in both directions —
   nothing reads the table until the API ships).
2. Merge `develop` → `main`. Railway auto-deploys the API.
3. `vercel --prod` for the frontend — Vercel is **not** git-connected.
4. Stamp the migration.
5. Prod smoke, then re-run the diagnosis.

## E. Post-deploy verification (the specific claims to re-prove)

1. Paste a LinkedIn URL → expect a real error naming the reason, **not** "Job added".
2. Generate a resume, edit it, Load an earlier version → content actually reverts.
3. Remove a job from a target → gone from that target on **both** sorts; still present in
   another target that also matched it; **still gone after a scoring cycle**.
4. Archived job absent from the default list on the `sort=score` RPC path.
5. Cover letter on a Skip job → warning first; with override → a real letter, no refusal.
6. Confirm on a second identity that a removal did not affect anyone else's list.

## F. Residual risk

- **Highest**: PR-3 touches two SQL RPCs on the hot list path. A wrong anti-join silently
  hides jobs. Mitigated by testing every read path and by the RPCs being independently
  revertible from the FE.
- **Prompt change** (PR-4) can shift cover-letter quality beyond the stretch case; the
  eval before/after is the gate, and the block is scoped to override-only calls.
- Fix-forward is the standing instruction if prod verification fails.
