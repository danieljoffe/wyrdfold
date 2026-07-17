-- perf(#365): cross-target job list RPC — kill the status-filter full-scan.
--
-- `_list_jobs_across_user_targets` (the untargeted dashboard + All-Jobs views)
-- couldn't push a status filter into the DB: per-user status lives in
-- `user_jobs` (absent → 'new'), so it fetched EVERY live scores row for the
-- user's targets (~18.5k rows × 3 JSONB columns) and resolved status + dedup +
-- sort + paginate in Python. Measured 7.7–8.9s server-side on prod, skirting
-- the 8s statement timeout and intermittently 57014-ing → the dashboard
-- Top-matches widget silently rendered empty on 500s.
--
-- The per-target path already solved this DB-side (`get_target_jobs`); this is
-- its cross-target twin. It mirrors, EXACTLY, the Python semantics it replaces:
--
--   * Best-representative dedup per job (a graded row beats a Pending one, then
--     higher RAW score wins — `_prefer_score_row`; raw, so decay never changes
--     the winner). Graded = `axis_scores` present, NOT `scoring_status`
--     ='complete' (which lies: 'complete' rows were never actually graded and
--     once surfaced ~2,300 keyword placeholders as fit scores — `_is_pending`).
--   * Off-family gate applied to the WINNING row's target (dedup first ignoring
--     family, then gate — matches `_gate_off_family(by_id)`), so a job hidden
--     because its best target is off-family stays hidden.
--   * Liveness (archived/purged/is_us) + Pending-exempt floor per row BEFORE
--     dedup (`_scores_live_join` + `_apply_score_floor`). The floor uses
--     `scoring_status` — a DIFFERENT signal from the graded bucket, on purpose.
--   * Score sort: graded rows always precede Pending ones; graded ordered by
--     the DISPLAYED score, Pending by recency (first_seen_at) — matches
--     `_rank_graded_first`. When p_recency_decay is true (prod has
--     RECENCY_DECAY_ENABLED on), the graded key is the age-decayed score
--     `score * max(0.3, 1 - max(0, age_days-7)*0.015)` — the exact
--     `compute_recency_score` multiplier. Ordering by the float matches the
--     read path's rounded display order (round is monotonic). The shared
--     `_apply_display_recency` post-step still sets the shown number; the RPC
--     only owns the order. Pending rows sort by recency regardless of decay.
--   * Returns `pending` per row so the wiring doesn't need `axis_scores`
--     (which the list selects otherwise avoid — it's the graded signal only).
--
-- SECURITY INVOKER (default — RLS on scores/jobs/user_jobs/targets governs, like
-- get_target_jobs / pipeline_counts). Offset-paginated to match the untargeted
-- Python path's cursor contract. The wiring falls back to the Python path for
-- cases this can't express DB-side (custom axis weights, logistics/location
-- filters, multi-word search) — see jobs.py.

-- Drop the prior signature if present (this migration was iterated to add the
-- p_recency_decay param; DROP makes the CREATE below re-runnable).
DROP FUNCTION IF EXISTS public.get_cross_target_jobs(
    uuid[], integer, text, text, text, text, boolean, integer, integer, uuid
);

CREATE OR REPLACE FUNCTION public.get_cross_target_jobs(
    p_target_ids uuid[],
    p_min_score integer DEFAULT 0,
    p_status text DEFAULT NULL,
    p_company text DEFAULT NULL,
    p_search text DEFAULT NULL,
    p_sort text DEFAULT 'score',
    p_ascending boolean DEFAULT false,
    p_limit integer DEFAULT 20,
    p_offset integer DEFAULT 0,
    p_user_id uuid DEFAULT NULL,
    p_recency_decay boolean DEFAULT false
) RETURNS TABLE(
    id uuid, external_id text, source_id uuid, title text,
    company_name text, location text, department text,
    absolute_url text, score integer, score_breakdown jsonb,
    scoring_status text, logistics_filters jsonb, status text, salary_text text,
    greenhouse_updated_at timestamp with time zone,
    first_seen_at timestamp with time zone,
    created_at timestamp with time zone,
    pending boolean
)
    LANGUAGE plpgsql STABLE
    SET search_path TO ''
    AS $func$
DECLARE
    v_dir        text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_graded_key text;
    v_order      text;
    v_sql        text;
BEGIN
    -- Order clause. For the score sort, replicate `_rank_graded_first`:
    -- graded bucket first (always, regardless of direction), graded rows by the
    -- displayed score, Pending rows by recency (first_seen_at; NULL treated as
    -- oldest via -infinity, mirroring the Python `or ""` coercion). Other sorts:
    -- a plain column order. v_dir is a whitelisted literal — no injection.
    IF p_recency_decay THEN
        -- Age-decayed display score = score * compute_recency_multiplier(age).
        -- COALESCE handles NULL first_seen_at as age 0 (fresh) like _age_days.
        v_graded_key :=
            'b.score * GREATEST(0.3, 1.0 - GREATEST(0.0, '
            || 'COALESCE(EXTRACT(EPOCH FROM (now() - jp.first_seen_at)) / 86400.0, 0.0) - 7.0'
            || ') * 0.015)';
    ELSE
        v_graded_key := 'b.score';
    END IF;

    IF p_sort = 'created_at' THEN
        v_order := format('jp.created_at %1$s, b.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'company_name' THEN
        v_order := format('jp.company_name %1$s, b.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'title' THEN
        v_order := format('jp.title %1$s, b.job_posting_id %1$s', v_dir);
    ELSE
        v_order := format(
            'b.is_graded DESC, '
            || 'CASE WHEN b.is_graded THEN ' || v_graded_key || ' END %1$s, '
            || 'CASE WHEN NOT b.is_graded THEN COALESCE(jp.first_seen_at, ''-infinity''::timestamptz) END %1$s, '
            || 'b.job_posting_id %1$s',
            v_dir
        );
    END IF;

    v_sql := format($q$
        WITH best AS (
            SELECT DISTINCT ON (s.job_posting_id)
                s.job_posting_id,
                s.target_id,
                s.score,
                s.score_breakdown,
                s.scoring_status,
                s.logistics_filters,
                (s.axis_scores IS NOT NULL
                 AND jsonb_typeof(s.axis_scores) = 'object'
                 AND s.axis_scores <> '{}'::jsonb) AS is_graded
            FROM public.scores s
            INNER JOIN public.jobs jp ON jp.id = s.job_posting_id
            WHERE s.target_id = ANY ($2)
                AND s.excluded = FALSE
                AND jp.archived_at IS NULL
                AND jp.purged_at IS NULL
                AND jp.is_us IS NOT FALSE
                AND ($3 IS NULL
                     OR s.scoring_status IS DISTINCT FROM 'complete'
                     OR s.score >= $3)
            ORDER BY s.job_posting_id,
                (s.axis_scores IS NOT NULL
                 AND jsonb_typeof(s.axis_scores) = 'object'
                 AND s.axis_scores <> '{}'::jsonb) DESC,
                s.score DESC
        )
        SELECT
            jp.id, jp.external_id, jp.source_id, jp.title,
            jp.company_name, jp.location, jp.department,
            jp.absolute_url, b.score, b.score_breakdown,
            b.scoring_status, b.logistics_filters,
            COALESCE(uj.status, 'new')::text AS status,
            jp.salary_text, jp.greenhouse_updated_at, jp.first_seen_at,
            jp.created_at,
            (NOT b.is_graded) AS pending
        FROM best b
        INNER JOIN public.jobs jp ON jp.id = b.job_posting_id
        INNER JOIN public.targets t ON t.id = b.target_id
        LEFT JOIN public.user_jobs uj
            ON uj.job_posting_id = jp.id AND uj.user_id = $1
        WHERE (t.role_family IS NULL OR jp.role_family IS NULL OR jp.role_family = t.role_family)
            AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
            AND ($5 IS NULL OR jp.company_name = $5)
            AND ($6 IS NULL OR jp.title ILIKE '%%' || $6 || '%%')
        ORDER BY %s
        LIMIT $7 OFFSET $8
    $q$, v_order);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset;
END;
$func$;

GRANT ALL ON FUNCTION public.get_cross_target_jobs(
    uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean
) TO anon;
GRANT ALL ON FUNCTION public.get_cross_target_jobs(
    uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean
) TO authenticated;
GRANT ALL ON FUNCTION public.get_cross_target_jobs(
    uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean
) TO service_role;
