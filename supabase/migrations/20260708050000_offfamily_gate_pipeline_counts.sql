-- #60/#278 follow-on (issue #282): family-gate pipeline_counts to match the list.
--
-- get_target_jobs got the off-family gate in 20260708040000, but pipeline_counts
-- (the per-status "New / Applied / ..." tallies) only got the is_us gate (#257),
-- never the family one. So the "New matches: N" count still tallies off-family
-- jobs and reads higher than the family-gated list — click "New" and the list is
-- shorter than the badge. Add the same per-target family clause: join the score's
-- target and keep a row only when the job's family matches the target's, the job
-- is untagged (role_family NULL), or the target is unclassified (NULL → ungated).
--
-- Recreated verbatim from 20260614160000_c3_global_archive (+ the #257 is_us
-- clause) with the targets join + family clause added. SECURITY INVOKER unchanged
-- (RLS still governs); CREATE OR REPLACE preserves grants.
CREATE OR REPLACE FUNCTION public.pipeline_counts(
    p_target_ids uuid[], p_min_score integer, p_user_id uuid DEFAULT NULL::uuid
) RETURNS TABLE(status text, count bigint)
    LANGUAGE sql STABLE
    SET search_path TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(uj.status, 'new') AS status, COUNT(DISTINCT s.job_posting_id)
  FROM   public.scores s
  JOIN   public.jobs j ON j.id = s.job_posting_id
  JOIN   public.targets t ON t.id = s.target_id
  LEFT JOIN public.user_jobs uj
    ON uj.job_posting_id = j.id AND uj.user_id = p_user_id
  WHERE  s.target_id = ANY (p_target_ids)
    AND  s.excluded = false
    AND  j.archived_at IS NULL
    AND  j.is_us IS NOT FALSE
    AND  (t.role_family IS NULL OR j.role_family IS NULL OR j.role_family = t.role_family)
    AND  (p_min_score IS NULL OR s.score >= p_min_score)
  GROUP BY COALESCE(uj.status, 'new');
$$;
