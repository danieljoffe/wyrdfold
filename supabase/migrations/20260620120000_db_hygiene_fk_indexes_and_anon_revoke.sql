-- #23 DB hygiene (audit Phase 3): cover uncovered FKs + drop an inert grant.
--
-- Part A — revoke total_spend_all_since from anon/authenticated.
--   total_spend_all_since(p_since) is SECURITY INVOKER and sums cost across
--   ALL users; it is only ever called by the global LLM circuit breaker on
--   the poll cycle via the service-role client (cost_log.total_spend_all).
--   Being INVOKER it is currently safe-but-wrong for anon/authenticated
--   (RLS would scope an authenticated caller to their own rows, anon to
--   none), but the standing grant is pointless attack surface and would
--   silently leak a global spend total if the function were ever flipped to
--   SECURITY DEFINER. Revoke it; service_role keeps EXECUTE. Mirrors #111.
--   REVOKE is idempotent, so this is re-runnable.
REVOKE ALL ON FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone)
  FROM "anon", "authenticated";

-- Part B — add covering indexes for foreign keys that have none.
--   Each column below is a declared FK with no leading index, so the FK's
--   ON DELETE CASCADE / SET NULL action and any anti-join on it seq-scan the
--   child table. Plain (non-CONCURRENTLY) builds to match the repo
--   convention and the txn-wrapped `supabase db push` path (see #108).
--   IF NOT EXISTS keeps this re-runnable.
--
--   Of these tables only `jobs` is "hot" per the #112 index-lock guard
--   (test_migration_safety.py). It is empty/tiny pre-launch, so the brief
--   SHARE lock during this single small index build is acceptable; the
--   CONCURRENTLY rebuild strategy for scale stays tracked in #112.
-- index-lock-ok: jobs is empty/tiny pre-launch; brief build-lock fine; concurrent rebuild at scale tracked in #112.
CREATE INDEX IF NOT EXISTS idx_user_targets_target
  ON public.user_targets (target_id);
CREATE INDEX IF NOT EXISTS idx_analyses_target
  ON public.analyses (target_id);
CREATE INDEX IF NOT EXISTS idx_job_feedback_job_posting
  ON public.job_feedback (job_posting_id);
CREATE INDEX IF NOT EXISTS idx_target_learning_log_target
  ON public.target_learning_log (target_id);
CREATE INDEX IF NOT EXISTS idx_jobs_llm_analysis
  ON public.jobs (llm_analysis_id);
CREATE INDEX IF NOT EXISTS idx_uploaded_resumes_prose_doc
  ON public.uploaded_resumes (prose_doc_id);
CREATE INDEX IF NOT EXISTS idx_experience_turns_prose_doc
  ON public.experience_conversation_turns (prose_doc_id);
CREATE INDEX IF NOT EXISTS idx_experience_optimized_prose_doc
  ON public.experience_optimized_docs (prose_doc_id);
