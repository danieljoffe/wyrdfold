-- #6 R2 (step 1): DB-enforced authorization for the user-facing scores write.
--
-- When a user runs an analysis (routers/analysis.py), the LLM blend updates the
-- shared (job, target) `scores` row + stamps `jobs.llm_analysis_id`. The shared
-- catalog has a USING(true) SELECT policy and NO write policy, so RLS denies
-- authenticated writes — the write ran via the service-role client with the
-- ownership check ("caller follows this target") living only in Python (#24 F2).
--
-- Move that write behind a SECURITY DEFINER RPC so Postgres enforces the
-- ownership rule regardless of caller — the first step of the uniform
-- write-API pattern for shared-catalog writes (#6 R2). Called from the
-- per-request user client (auth.uid() = the caller). A service-role caller
-- (poller/operator, auth.uid() NULL) is exempt; the background scorer keeps
-- using bulk_update_scores.
--
-- Grants: authenticated + service_role only. PUBLIC is revoked — Postgres
-- grants EXECUTE to PUBLIC on every new function by default, which would
-- otherwise let anon call it (see 20260621120000).
CREATE OR REPLACE FUNCTION public.user_apply_score_blend(
    p_job_posting_id uuid,
    p_target_id uuid,
    p_score integer,
    p_analysis_id uuid
) RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path TO 'public', 'pg_catalog'
AS $$
BEGIN
    -- Ownership gate mirroring the user_targets RLS policy
    -- ((select auth.uid())::text = user_id). A JWT caller may only blend a
    -- score for a target they follow; service-role (auth.uid() NULL) bypasses,
    -- matching the poller/operator path.
    IF auth.uid() IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.user_targets
        WHERE user_id = (SELECT auth.uid())::text
          AND target_id = p_target_id
    ) THEN
        RAISE EXCEPTION 'not authorized to score target %', p_target_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    UPDATE public.scores
       SET score = p_score,
           scoring_status = 'complete'
     WHERE job_posting_id = p_job_posting_id
       AND target_id = p_target_id;

    UPDATE public.jobs
       SET llm_analysis_id = p_analysis_id
     WHERE id = p_job_posting_id;
END;
$$;

ALTER FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid)
  TO authenticated, service_role;
