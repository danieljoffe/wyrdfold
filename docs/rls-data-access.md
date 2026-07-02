# RLS data-access map (#88 / #6)

Which Supabase client each API surface runs on, and **why the service-role
paths are service-role on purpose**. This is the finish line for #88: when a
path below says "intentional", migrating it is not pending work — changing it
needs a design decision, not a cleanup PR.

Two clients exist (`app/dependencies.py`):

| Client                                                   | Dependency                                                                                                        | RLS                     |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------- |
| **User client** — per-request, bound to the caller's JWT | `get_user_supabase` (JWT-only) / `get_supabase_for_caller` (dual-auth: JWT → user client, api-key → service-role) | enforced (`auth.uid()`) |
| **Service-role** — singleton, bypasses RLS               | `get_supabase`                                                                                                    | bypassed                |

## Migrated — runs on the RLS user client

| Surface                                                                               | Tables                                                                     | Since                   |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ----------------------- |
| `experience.py` (all endpoints)                                                       | `experience_*`, `conversation` turns, preferences                          | #88 Phase 1 (#157–#162) |
| `tailor.py` reads + document writes                                                   | `uploaded_resumes`, `documents`, `document_versions`                       | #88 Phase 1 (#163)      |
| `status.py` (history read, status write)                                              | `user_jobs`, `status_log`                                                  | #88 Phase 2 (#167)      |
| `targets.py` prefs surface (axis-weights ×3, notification-thresholds, preferences ×2) | `user_targets`                                                             | #88 Phase 2 (#167)      |
| `insights.py` (all three)                                                             | reads: `user_jobs`, `status_log`, `analyses`, `llm_costs` + shared catalog | #88 Phase 3             |
| `jobs.py` `DELETE /{id}` (per-user archive)                                           | `user_jobs`                                                                | #88 Phase 3             |
| `feedback.py` create/remove/list, learning-log read                                   | `job_feedback`, `target_learning_log`                                      | #79 R1                  |
| `user_profile.py` (all but account-delete/export)                                     | `user_profiles`                                                            | #79                     |

## Intentionally service-role — with the reason

| Surface                                                                                                                                                                    | Why it cannot / must not use the user client                                                                                                                                                                                                                                                                                              |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Cost ledger writes** (`llm_costs` in derive/consolidate/turn/probe/chunks)                                                                                               | `llm_costs` deliberately has **no INSERT policy** for `authenticated`: a user able to write cost rows could forge negative costs and bypass budget enforcement. Endpoints thread a separate service-role `cost_supabase` alongside the RLS client (the Phase-1 dual-client pattern).                                                      |
| **Shared-catalog writes** (`jobs`, `targets`, `scores`, `reference_jds` — learner endpoints in `feedback.py`, target CRUD/activation in `targets.py`, manual-add, rescore) | The shared catalog has read-only policies and **no write policy by design** — N users share one target/posting, so "the caller's rows" is undefined. Writes go through service-role + Python ownership guards, or SECURITY DEFINER RPCs (`user_apply_score_blend`, vote tally). The open decision on hardening this further is **#6 R2**. |
| **Operator / cron surfaces** (`admin.py`, `poll.py`, `sources.py`, `discovery.py`, rescore/backfill in `jobs.py`)                                                          | Api-key callers have no JWT — there is no user to bind. Gated by `verify_api_key` instead.                                                                                                                                                                                                                                                |
| **Account deletion** (`user_profile.py DELETE /account`)                                                                                                                   | Spans every per-user table **plus** the Supabase auth admin API (deleting the `auth.users` row) — inherently privileged.                                                                                                                                                                                                                  |
| **Data export** (`user_profile.py GET /export`)                                                                                                                            | Reads every per-user table + Storage in one pass; kept on service-role while the export is reworked to stream (#29 H-r2-4). Candidate to ride the user client after that rework.                                                                                                                                                          |
| **Pre-auth surfaces** (`waitlist.py`, `keys.py` validation)                                                                                                                | No JWT exists yet at call time.                                                                                                                                                                                                                                                                                                           |
| **`reference_jds.user_id`** (nullable uuid, FK `ON DELETE SET NULL`)                                                                                                       | Shared-catalog content with **optional attribution** — NULL means "seeded/unattributed" and is load-bearing: the #29 erasure flow anonymizes (keeps the shared JD, drops the link), and the SET NULL FK automates + backstops exactly that. Deliberately NOT NOT-NULL/CASCADE (decision on #6, 2026-07-02, migration `20260702150000`).   |
| **Background pipeline** (poller, scheduler, batch, learner internals)                                                                                                      | No request context — these run from cron/queue with the service key, charging work to the activating user via explicit `user_id` params.                                                                                                                                                                                                  |

## Dual-auth (JWT → RLS, api-key → service-role): deferred, with reason

`jobs.py` list/read surfaces (`GET /jobs`, `/pipeline-counts`, `GET /{id}`) and
`analysis.py` remain on `get_supabase` + `get_current_user_id_optional` even
though a JWT caller _could_ be routed through the user client via
`get_supabase_for_caller`:

- The list path calls the `get_target_jobs` **RPC** — routing it through the
  user client needs an EXECUTE grant + in-function scoping audit first.
- It is the hottest path in the app; flipping its client should be validated
  against a live stack, not shipped blind.

If you pick this up: switch the dependency to `get_supabase_for_caller`,
verify the RPC grant, and extend `tests/integration/test_rls_jobs_reads.py`
to run the list through a user JWT.
