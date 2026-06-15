-- #101 perf: push the per-target `scores` aggregation in compute_targets
-- (app/services/insights.py) into one server-side GROUP BY pass.
--
-- compute_targets' scoped path (target_ids is not None — the only path the
-- /insights/targets router ever takes) previously:
--   1. read `scores` to resolve membership (~11k rows),
--   2. read `jobs` for the windowed postings (~11k rows),
--   3. read `user_jobs` for the per-user status overlay (~11k rows),
--   4. read `scores` AGAIN to build the (posting,target)->score lookup (~11k),
-- then in Python folded those ~33k+ rows into: per-target metrics
-- (job_count / avg_score / applied / interview), a 10-bucket score
-- distribution, an unscored count, and a weekly score trend.
--
-- This RPC returns all of that from a SINGLE pass over the same `scores` set
-- (no double-read), collapsing ~33k rows of transfer to one small jsonb
-- (a handful of targets + 10 buckets + a few weeks + 1 int).
--
-- It mirrors the existing read RPCs (insights_pipeline_status_counts /
-- get_target_jobs / pipeline_counts): LANGUAGE sql STABLE, fixed search_path,
-- owned by postgres, granted to anon/authenticated/service_role. It also
-- mirrors spend_by_purpose_since by returning jsonb so all three result sets
-- (targets / distribution / trend) ride one round trip.
--
-- BYTE-IDENTITY CONTRACT (must match the Python it replaces exactly):
--   * base relation = one row per non-excluded `scores` row whose target_id is
--     in p_target_ids AND whose target exists in `targets` (mirrors the
--     `if tid not in target_labels: continue` guard) AND whose job is in the
--     window (created_at >= p_since; NULL p_since = open). The
--     UNIQUE(job_posting_id, target_id) constraint on `scores` means each
--     (pid, tid) appears once, exactly like the Python score_lookup dict.
--   * score := COALESCE(s.score, 0)::int  (mirrors int(r.get("score") or 0)).
--   * status := COALESCE(uj.status, 'new'), uj joined on the caller's user_id
--     (#75 "absent = new"; p_user_id NULL never matches → all 'new').
--   * Per-target aggregates are RAW (job_count, score_sum, score_n,
--     applied_count, interview_count) — averages and conversion_rate are
--     rounded in PYTHON. Postgres round() is half-away-from-zero while Python
--     round() is banker's (half-to-even); rounding in SQL would diverge, so we
--     return raw SUM/COUNT and let Python reproduce the byte-identical number.
--   * distribution: only scores <> 0 contribute (mirrors the `if score:`
--     gate); bucket index = LEAST(GREATEST(LEAST(score,100),0)/10, 9) using
--     integer division (exact — integer bucketing has no rounding issue).
--   * unscored = COUNT(DISTINCT posting) where NONE of that posting's rows had
--     a nonzero score (mirrors `if not seen_any_score`).
--   * trend: per posting take MAX(score) across its targets; keep it only when
--     > 0; group by date_trunc('week', created_at)::date (Monday, computed in
--     the session timezone — the SAME timezone PostgREST serializes created_at
--     in, so it matches Python's _iso_week_start(_parse_dt(created_at)) for any
--     session tz). Returns RAW score_sum/score_n per week; Python rounds.
--   * ASYMMETRY (faithfully preserved): the per-target metrics, distribution
--     and unscored count are computed over memberships whose target EXISTS in
--     `targets` (the Python `if tid not in target_labels: continue` guard),
--     whereas the trend's per-posting MAX uses the RAW membership set (Python
--     reads `membership.get(pid)` there without the target_labels filter). In
--     practice the caller's target_ids always exist in `targets`, so the two
--     bases coincide — but they are split here so a stale/orphaned target_id
--     can't make the RPC diverge from the Python it replaces.
CREATE OR REPLACE FUNCTION "public"."insights_targets_groupby"(
    "p_target_ids" "uuid"[],
    "p_since" timestamp with time zone,
    "p_user_id" "uuid" DEFAULT NULL::"uuid"
) RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  WITH member AS (
    -- Raw membership: one row per non-excluded (posting, target) score in the
    -- window, NO targets join (mirrors Python's `membership` / `score_lookup`,
    -- which are not filtered by target existence). Drives the trend's per-
    -- posting MAX exactly as the Python does.
    SELECT s.job_posting_id           AS posting_id,
           s.target_id                AS target_id,
           COALESCE(s.score, 0)::int  AS score,
           j.created_at               AS created_at
    FROM   public.scores s
    JOIN   public.jobs   j ON j.id = s.job_posting_id
    WHERE  s.target_id = ANY (p_target_ids)
      AND  s.excluded = false
      AND  (p_since IS NULL OR j.created_at >= p_since)
  ),
  base AS (
    -- target_labels-filtered membership (the `if tid not in target_labels:
    -- continue` guard) with the per-user status overlaid. Mirrors
    -- target_jobs[tid] entries 1:1; drives per-target metrics, distribution
    -- and the unscored count.
    SELECT m.posting_id,
           m.target_id,
           t.label                       AS label,
           m.score                       AS score,
           COALESCE(uj.status, 'new')    AS status
    FROM   member m
    JOIN   public.targets t ON t.id = m.target_id
    LEFT JOIN public.user_jobs uj
      ON uj.job_posting_id = m.posting_id AND uj.user_id = p_user_id
  ),
  per_target AS (
    SELECT target_id,
           label,
           COUNT(*)                                                AS job_count,
           SUM(score)::bigint                                      AS score_sum,
           COUNT(*)                                                AS score_n,
           COUNT(*) FILTER (
             WHERE status IN ('applied', 'interviewing', 'offer')
           )                                                       AS applied_count,
           COUNT(*) FILTER (
             WHERE status IN ('interviewing', 'offer')
           )                                                       AS interview_count
    FROM   base
    GROUP BY target_id, label
  ),
  distribution AS (
    -- Only nonzero scores contribute (the Python `if score:` gate). Integer
    -- bucketing is exact, so it is safe to compute server-side.
    SELECT LEAST(GREATEST(LEAST(score, 100), 0) / 10, 9) AS bucket_idx,
           COUNT(*)                                       AS count
    FROM   base
    WHERE  score <> 0
    GROUP BY LEAST(GREATEST(LEAST(score, 100), 0) / 10, 9)
  ),
  unscored_posting AS (
    -- One row per posting (target_labels-filtered) recording whether it saw
    -- any nonzero score. unscored = those that saw none (Python's
    -- `if not seen_any_score`).
    SELECT posting_id,
           bool_or(score <> 0) AS seen_any_score
    FROM   base
    GROUP BY posting_id
  ),
  trend_posting AS (
    -- One row per posting with its best RAW-membership score and the job's
    -- week (Python's per-posting `best = max(per_target, default=0)`).
    SELECT posting_id,
           MAX(score)     AS best_score,
           MAX(created_at) AS created_at
    FROM   member
    GROUP BY posting_id
  ),
  trend AS (
    SELECT date_trunc('week', created_at)::date AS week_start,
           SUM(best_score)::bigint              AS score_sum,
           COUNT(*)                              AS score_n
    FROM   trend_posting
    WHERE  best_score > 0
    GROUP BY date_trunc('week', created_at)::date
  )
  SELECT jsonb_build_object(
    'targets', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'target_id',       target_id,
               'label',           label,
               'job_count',       job_count,
               'score_sum',       score_sum,
               'score_n',         score_n,
               'applied_count',   applied_count,
               'interview_count', interview_count
             ))
      FROM per_target
    ), '[]'::jsonb),
    'distribution', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'bucket_idx', bucket_idx,
               'count',      count
             ))
      FROM distribution
    ), '[]'::jsonb),
    'trend', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'week_start', week_start,
               'score_sum',  score_sum,
               'score_n',    score_n
             ))
      FROM trend
    ), '[]'::jsonb),
    'unscored', COALESCE((
      SELECT COUNT(*) FROM unscored_posting WHERE seen_any_score = false
    ), 0)
  );
$$;

ALTER FUNCTION "public"."insights_targets_groupby"(
    "uuid"[], timestamp with time zone, "uuid"
) OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."insights_targets_groupby"(
    "uuid"[], timestamp with time zone, "uuid"
) TO "anon";
GRANT ALL ON FUNCTION "public"."insights_targets_groupby"(
    "uuid"[], timestamp with time zone, "uuid"
) TO "authenticated";
GRANT ALL ON FUNCTION "public"."insights_targets_groupby"(
    "uuid"[], timestamp with time zone, "uuid"
) TO "service_role";
