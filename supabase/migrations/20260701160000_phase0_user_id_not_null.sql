-- Phase 0 (deployment-modes): user_id NOT NULL on the uuid per-user tables.
--
-- This is the invariant Phase 0 has been building toward: every per-user row has
-- a non-null owner. By the time this runs (after the earlier Phase 0 migrations,
-- by timestamp order) every one of these tables is 0-NULL:
--   * llm_costs — backfilled NULL→SYSTEM (20260701140000)
--   * analyses  — stale NULL rows deleted (20260701150000)
--   * everything else was already 0-NULL in prod (verified 2026-07-01); the
--     is_("user_id","null") service branches that used to write NULLs are gone.
--
-- One straggler: status_log has a single legacy single-tenant row (a job
-- new→saved transition from 2026-05-29, no user recorded). It's an audit entry,
-- so we keep it — re-owned to SYSTEM (the "no specific user" principal) rather
-- than deleted — which also brings status_log to 0-NULL.
--
-- SET NOT NULL takes a brief ACCESS EXCLUSIVE lock to scan for NULLs; these
-- tables are small enough that it's instant. Reversible (DROP NOT NULL) if ever
-- needed. The account-cascade FK + the text-user_id tables (user_targets,
-- job_feedback, contribution_votes, target_learning_log) + reference_jds (which
-- may hold legitimate global NULLs) are handled separately.

-- 1. Re-own the lone legacy status_log row.
UPDATE public.status_log
SET user_id = '00000000-0000-0000-0000-000000000001'
WHERE user_id IS NULL;

-- 2. Enforce the invariant on the uuid per-user tables.
ALTER TABLE public.analyses                     ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.batch_runs                   ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.documents                    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.experience_conversation_turns ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.experience_optimized_docs    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.experience_preferences       ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.experience_prose_docs        ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.llm_costs                     ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.status_log                    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.uploaded_resumes             ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.user_profiles                ALTER COLUMN user_id SET NOT NULL;
