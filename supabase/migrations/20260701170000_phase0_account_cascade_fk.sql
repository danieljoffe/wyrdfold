-- Phase 0 (deployment-modes): account-cascade FK on the uuid per-user tables.
--
-- Adds user_id -> auth.users(id) ON DELETE CASCADE to every uuid per-user table.
-- This is the defense-in-depth the design settled on: the #29 app erasure flow
-- stays authoritative (it also purges Storage + external state a DB cascade
-- can't), and this FK is a DB-enforced backstop so no per-user row can outlive
-- its owner or reference a non-existent one. Deleting an auth.users row now
-- cascades to all their data automatically.
--
-- Safe by construction at this point in the Phase 0 sequence:
--   * SYSTEM is a real auth.users row (20260701130000), so SYSTEM-owned rows
--     (backfilled llm_costs, status_log) satisfy the FK.
--   * user_id is NOT NULL on these tables (20260701160000) and prod has no
--     orphan ids (verified 2026-07-01: every non-null user_id is a real user).
-- ADD CONSTRAINT validates existing rows; all pass.
--
-- Scope: the 13 uuid tables. The text-user_id tables (user_targets, job_feedback,
-- contribution_votes, target_learning_log) need a text->uuid conversion first
-- and are handled separately; reference_jds pends a global-NULL design call.

ALTER TABLE public.analyses                      ADD CONSTRAINT analyses_user_id_fkey                      FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.batch_runs                    ADD CONSTRAINT batch_runs_user_id_fkey                    FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.documents                     ADD CONSTRAINT documents_user_id_fkey                     FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.experience_conversation_turns ADD CONSTRAINT experience_conversation_turns_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.experience_optimized_docs     ADD CONSTRAINT experience_optimized_docs_user_id_fkey     FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.experience_preferences        ADD CONSTRAINT experience_preferences_user_id_fkey        FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.experience_prose_docs         ADD CONSTRAINT experience_prose_docs_user_id_fkey         FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.llm_costs                      ADD CONSTRAINT llm_costs_user_id_fkey                      FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.status_log                     ADD CONSTRAINT status_log_user_id_fkey                     FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.uploaded_resumes              ADD CONSTRAINT uploaded_resumes_user_id_fkey              FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.user_profiles                 ADD CONSTRAINT user_profiles_user_id_fkey                 FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.user_api_keys                 ADD CONSTRAINT user_api_keys_user_id_fkey                 FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.user_jobs                     ADD CONSTRAINT user_jobs_user_id_fkey                     FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
