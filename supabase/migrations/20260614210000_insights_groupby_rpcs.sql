-- #101 perf: push the insights pipeline status aggregation into a server-side
-- GROUP BY so the row set never leaves Postgres.
--
-- compute_pipeline (app/services/insights.py) previously fetched every posting
-- under the caller's targets within the window (~11k jobs rows at beta scale),
-- overlaid the per-user status from a second ~11k-row user_jobs pull, then
-- COUNTed by status in Python — for BOTH the funnel and the _kpis_from status
-- distribution. This RPC returns the per-status tally directly (~9 rows).
--
-- It mirrors the existing read RPCs (get_target_jobs / pipeline_counts):
-- LANGUAGE sql STABLE, fixed search_path, owned by postgres, granted to
-- anon/authenticated/service_role.
--
-- Semantics match _fetch_postings_window + the Python status counters exactly:
--   * membership: postings with a non-excluded score against any of the
--     caller's targets (mirrors _posting_target_map's `excluded = false`);
--   * COUNT(DISTINCT s.job_posting_id) so a posting scored against several of
--     the caller's targets counts once (mirrors set(membership.keys()));
--   * created_at window is `>= p_since` / `< p_until` (mirrors .gte()/.lt());
--     a NULL bound means "open on that side" (the `all`/current-window cases);
--   * per-user status is COALESCE(uj.status, 'new') — a posting the caller
--     never touched has no user_jobs row and reads as 'new' (#75 "absent =
--     new"); p_user_id NULL never matches the join, so all rows read 'new'.
--
-- NOTE: this RPC intentionally has NO p_min_score floor (unlike the #75-owned
-- pipeline_counts RPC) — the insights funnel applies no score floor — so it is
-- a SEPARATE function rather than an extension of pipeline_counts (whose body
-- is owned by #75, per #93).
CREATE OR REPLACE FUNCTION "public"."insights_pipeline_status_counts"(
    "p_target_ids" "uuid"[],
    "p_since" timestamp with time zone,
    "p_until" timestamp with time zone,
    "p_user_id" "uuid" DEFAULT NULL::"uuid"
) RETURNS TABLE("status" "text", "count" bigint)
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(uj.status, 'new') AS status, COUNT(DISTINCT s.job_posting_id)
  FROM   public.scores s
  JOIN   public.jobs j ON j.id = s.job_posting_id
  LEFT JOIN public.user_jobs uj
    ON uj.job_posting_id = j.id AND uj.user_id = p_user_id
  WHERE  s.target_id = ANY (p_target_ids)
    AND  s.excluded = false
    AND  (p_since IS NULL OR j.created_at >= p_since)
    AND  (p_until IS NULL OR j.created_at <  p_until)
  GROUP BY COALESCE(uj.status, 'new');
$$;

ALTER FUNCTION "public"."insights_pipeline_status_counts"(
    "uuid"[], timestamp with time zone, timestamp with time zone, "uuid"
) OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."insights_pipeline_status_counts"(
    "uuid"[], timestamp with time zone, timestamp with time zone, "uuid"
) TO "anon";
GRANT ALL ON FUNCTION "public"."insights_pipeline_status_counts"(
    "uuid"[], timestamp with time zone, timestamp with time zone, "uuid"
) TO "authenticated";
GRANT ALL ON FUNCTION "public"."insights_pipeline_status_counts"(
    "uuid"[], timestamp with time zone, timestamp with time zone, "uuid"
) TO "service_role";
