# Onboarding flow sweep — 2026-08-14

Four live runs of the prod onboarding wizard (wyrdfold.com, borrowed-account access,
re-entered each time via Settings → Account → **Redo onboarding**): Path B end-to-end
(fresh resume upload → suggestions → created 2 targets), Path C end-to-end
(conversation → mid-flow refresh → completed with zero targets), Path A end-to-end
(keep-existing-file → Workday job URL → tailored resume), and a fourth run to probe
the exit links. Everything below was observed live; file references were traced
afterwards in this repo.

Since the 2026-08-13 walkthrough this flow has visibly improved: step-level "Skip this
step" now advances instead of abandoning, the counter no longer counts the path
chooser, and the Path A payoff (tailored resume) fires — no 404. All three paths
complete with no hard flow-stopper.

Session side effects (owner account): 3 targets created **and left active** ("Staff
Full-Stack Engineer", "Founding Engineer", "Senior Full Stack Engineer" from the
Humana posting — deactivate any unwanted ones; active targets consume scoring
allowance each poll cycle), 1 imported job + tailored-resume draft (~$0.07), onboarding
marked complete.

---

## Findings

### A. Product bugs / broken promises (confirmed by reproduction)

| #   | Finding                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Evidence / mechanism                                                                                                                                                                                                                                                                            |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A1  | **"Finish setup later" is permanent — there is no later.** Both global exits ("Skip setup for now" on the chooser, "Finish setup later" mid-flow) POST `/onboarding/complete`; `/onboarding` then bounces to the dashboard forever. Mid-flow resume support (#85) only benefits users who close the tab; anyone using the labeled exit loses their place, and the only road back is the buried Settings reset. Clicking it from Path B's resume step also lands on **/targets** with zero explanation of where you are. | Exits are wired this way _because_ the dashboard gate (`dashboard/page.tsx:200`) redirects any `completed_at == null` profile back to `/onboarding` — without a third state, not-completing means being trapped in the wizard (the old "skip doesn't stick" loop, see `completeOnboarding.ts`). |
| A2  | **The completion screen lies when nothing was created.** Deselect all suggestions → "Continue without targets" (good) → identical "You're all set! Head to your targets to start tracking jobs and generating tailored resumes." + "Go to Targets". A fresh user who skipped resume + targets is not set up at all: no matching will ever run, and the CTA lands on an empty page.                                                                                                                                      | `CompletionScreen.tsx` is static, takes no props; the wizard/`TargetSuggestions` knows `createdCount` but doesn't thread it through.                                                                                                                                                            |
| A3  | **Refreshing at pick-targets rerolls the suggestions.** Resume-at-step works, but the suggest call re-runs from scratch: my pre-refresh set of 2 became a different set of 1 after reload — options silently vanish, plus a re-billed LLM pass and a fresh ~20 s wait.                                                                                                                                                                                                                                                  | `TargetSuggestions.tsx` POSTs `/api/targets/suggest` on every mount; no cache anywhere.                                                                                                                                                                                                         |
| A4  | **Near-duplicate suggestions escape the dedup gate.** Post-refresh it offered "Founding Engineer / Head of Engineering" while the account already followed "Founding Engineer". The matcher (exact + pg_trgm ≥ 0.7, already-followed dropped — `services/targets/match.py`) misses word-extension labels: the extra words dilute trigram similarity below 0.7 even though one label contains the other.                                                                                                                 | `match.py:_SIMILARITY_THRESHOLD`; containment case not handled.                                                                                                                                                                                                                                 |
| A5  | **Company junk at the Path A wow moment.** The tailored-resume header reads "Senior Full Stack Engineer — **003 Humana Inc.**" and the junk propagates into the export filename (`daniel-joffe-003-humana-inc-2026-08-14.docx`). This is the first artifact a new user would show another human. Titles got the `title_display` treatment (#728/#729/#731); `company_name` renders raw.                                                                                                                                 | `ResumeReviewPage.tsx:120` (filename slug) and `:674` (subtitle) render `posting.company_name` verbatim; junk originates at feed ingest. Stored cleanup is unsafe — company_name is a dedup-key input (see decisions).                                                                          |

### B. Friction (real but not broken)

| #   | Finding                                                                                                                                                                                                                                              | Where                                                                                           |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| B1  | **No back navigation.** A mis-clicked path card is a commitment: no way back to the chooser (or any earlier step) — only forward skips or the global exit.                                                                                           | `OnboardingWizard.tsx` — steps only ever call `goNext`.                                         |
| B2  | Path A silently skips pick-targets and the completion screen (jumps to the resume review page). Defensible — it _is_ the promised payoff — but the user never gets the orientation moment. Onboarding is still correctly marked complete (verified). | `TargetSuggestions.draftPathAResume` → `router.push` to the review page.                        |
| B3  | Path C's opener is heavy jargon when a saved experience doc exists ("How many candidates did the LLM pipelines process…?"). The "Why this question" caption helps. True blank-slate Path C untested from this account.                               | Conversation prompts — **owner-scoped**: prompt edits trip spend-bearing evals; #726 in flight. |

### C. Retractions / downgrades from earlier notes

- "Entered at Step 2 of 4" (identity auto-skip) is a **test-account artifact**:
  `IdentityStep` only auto-advances when the profile already has name+email. A true
  new user sees identity as Step 1. No fix needed.
- "No dedup against existing targets" (first-pass read) was wrong as stated — the
  matcher exists and works for exact/fuzzy cases; the real gap is the containment
  case (A4).

### D. What works well (keep as-is)

Reset drops straight into the wizard; staged, honest progress copy at every LLM wait
("this can take up to a minute…", "keeps running if you navigate away"); live CTA
count ("Create 2 targets" → "Create 1 target" → "Continue without targets"); invalid
job-URL input caught client-side before any network call; Workday ingestion worked
first try; the Path C conversation caught an ambiguity in my answer and asked a real
clarifying question, and "Skip this question" pivots gracefully; every exit route
leaves a consistent server state — no stuck half-onboarded accounts observed.

---

## Plan

Priority order. Each item is its own PR into `develop`; FE PRs run `nx test wyrdfold`

- lint, API PRs run pytest + ruff + mypy. Copy changes get a `wyrdfold-e2e` grep
  (specs assert copy jest misses).

### P1 — Make "Finish setup later" true (A1) — DB + API + FE

Introduce a third state: **deferred**.

- Migration (additive, safe to apply before merge):
  `ALTER TABLE user_profiles ADD COLUMN onboarding_deferred_at timestamptz;`
- API (`routers/user_profile.py`): `POST /profile/onboarding/defer` sets it
  (idempotent); `GET /profile/onboarding` returns it; `complete` and `reset` clear it.
- FE: wizard's global exits call defer (same confirmed-persist contract as
  `completeOnboarding` — non-2xx → inline retry, never navigate unconfirmed), then land
  on **/dashboard** (not /targets). Dashboard gate becomes: redirect to `/onboarding`
  only when _neither_ `completed_at` nor `deferred_at` is set (fail-open behavior on
  degraded reads unchanged). While deferred-not-completed, the dashboard shows a
  dismiss-proof "Finish your setup" banner linking `/onboarding`, which still resumes
  mid-flow (#85) because `completed_at` stays NULL.
- No backfill: users who exited under the old wiring are `completed` and stay so.

### P2 — Completion screen tells the truth (A2) — FE only

Thread what actually happened into `CompletionScreen` (targets created count; whether
an experience doc exists). Zero-setup variant: honest copy ("Nothing set up yet —
matching won't start until you add a target") + primary CTA "Add a target"; keep the
current copy for the ≥1-target case. Check e2e copy assertions.

### P3 — Suggestions survive refresh + containment dedup (A3, A4)

- FE: cache the last `MatchedSuggestions` payload (sessionStorage) on success; reuse
  on remount; clear on create/complete; small "Refresh suggestions" affordance for a
  deliberate reroll.
- API (`match.py`): before the trigram check, drop/link suggestions whose normalized
  label contains — or is contained by, at word boundaries — a target the user already
  follows. In-memory against the user's own labels only; catalog-wide matching
  unchanged (0.7 rationale documented there stands).

### P4 — Company display junk on the resume surface (A5) — FE, scoped

Display-side `cleanCompanyDisplay()` used by the resume review subtitle + filename
slug. Conservative rule validated against real prod `company_name` values before
shipping (must not touch "3M", "37signals", "7-Eleven"); keep-original-if-empty guard.
**Durable follow-up (owner call): `company_display` column mirroring the
`title_display` pattern** — ingest-time cleaner + backfill + RPC plumbing; deferred
here because it touches prod schema/backfill during a release window.

### P5 — Wizard back navigation (B1) — FE only

"← Change path" affordance on the first post-chooser step (before any work is done);
resets persisted path/step to the chooser via the existing step PATCH.

### Deliberately not doing

- Path C prompt tone (B3): owner-scoped — prompt edits trip `test_prompt_regression`
  → spend-bearing evals + golden re-baseline; #726 already touches these prompts.
- Path A completion-moment (B2): decision note only; current behavior matches the
  path's promise.
- Step counter cosmetics: retracted (C).
