-- Perf (#47 follow-on): floored users (list_min_score set) bypassed the
-- pipeline_counts RPC entirely, because its raw ``score >= p_min_score`` floor
-- couldn't exempt Pending rows — every dashboard load for such a user ran the
-- Python fallback instead: one scores read + 2×ceil(N/150) chunked jobs +
-- user_jobs round-trips, ~1.6–2.6s measured on prod (2026-07-16 browser
-- audit), inside the dashboard's server render.
--
-- Teach the RPC the list's exact floor semantics (_apply_score_floor): the
-- floor is a fit-quality bar, so it applies only to rows that actually HAVE a
-- fit score (scoring_status = 'complete'); Pending rows hold only a keyword
-- placeholder and always pass. ``IS DISTINCT FROM`` covers the NULL
-- scoring_status arm. Also add the ``purged_at`` liveness arm for parity with
-- the list's _gate_live_us (the RPC predates purge, #79). Bonus parity win:
-- floored counts now ride the family-gated RPC, so they pick up the
-- off-family clause (#282) the Python fallback never had.
--
-- Recreated verbatim from 20260708050000_offfamily_gate_pipeline_counts with
-- those two clauses changed. SECURITY INVOKER unchanged (RLS still governs);
-- CREATE OR REPLACE preserves grants.
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
    AND  j.purged_at IS NULL
    AND  j.is_us IS NOT FALSE
    AND  (t.role_family IS NULL OR j.role_family IS NULL OR j.role_family = t.role_family)
    AND  (p_min_score IS NULL
          OR s.scoring_status IS DISTINCT FROM 'complete'
          OR s.score >= p_min_score)
  GROUP BY COALESCE(uj.status, 'new');
$$;
