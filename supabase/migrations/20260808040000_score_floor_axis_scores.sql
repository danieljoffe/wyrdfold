-- Score floor: key the Pending exemption on the real graded signal.
--
-- ``pipeline_counts`` mirrors ``_apply_score_floor`` in app/routers/jobs.py.
-- Both used ``scoring_status IS DISTINCT FROM 'complete'`` to decide which rows
-- are exempt from the floor. That signal is unreliable — ``_is_pending``'s own
-- docstring says so — and it disagreed with the badge the UI renders on
-- **5,130 live rows** (3,265 ``stage2`` + 1,865 ``stage1`` carrying real
-- ``axis_scores``). Those rows were exempted from the floor while the list
-- showed them as ordinary graded results, so "Score 85+" returned rows scored
-- 62 — and because the genuinely-complete high scorers WERE floored, raising
-- the bar made the list worse.
--
-- ``axis_scores`` is Phase 2's atomic write and ``_is_pending``'s primary
-- signal, so keying on it makes the floor agree exactly with the Pending badge.
-- ``axis_scores = '{}'`` has zero rows in prod (verified 2026-08-08), so
-- ``IS NULL`` is an exact match for the Python classifier's emptiness rung.
--
-- Nothing else in the function changes. Keep this in step with
-- ``_apply_score_floor``: a divergence means the dashboard tiles and the job
-- list disagree about the same floor.

CREATE OR REPLACE FUNCTION public.pipeline_counts(
  p_target_ids uuid[],
  p_min_score integer,
  p_user_id uuid DEFAULT NULL::uuid
)
RETURNS TABLE(status text, count bigint)
LANGUAGE sql
STABLE
SET search_path TO 'public', 'pg_catalog'
AS $function$
  SELECT COALESCE(uj.status, 'new') AS status, COUNT(DISTINCT s.job_posting_id)
  FROM   public.scores s
  JOIN   public.jobs j ON j.id = s.job_posting_id
  JOIN   public.targets t ON t.id = s.target_id
  LEFT JOIN public.user_jobs uj
    ON uj.job_posting_id = j.id AND uj.user_id = p_user_id
  WHERE  s.target_id = ANY (p_target_ids)
    AND  s.excluded = false
    AND  j.archived_at IS NULL
    AND  j.purged_at IS NULL
    AND  j.is_us IS NOT FALSE
    AND  (t.role_family IS NULL OR j.role_family IS NULL OR j.role_family = t.role_family)
    AND  (p_min_score IS NULL
          OR s.axis_scores IS NULL
          OR s.score >= p_min_score)
  GROUP BY COALESCE(uj.status, 'new');
$function$;
