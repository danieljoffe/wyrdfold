-- perf(#365): fix the candidate blow-up from 20260717020000.
--
-- That migration made the scores dedup index-only (good) but moved the live
-- filter OUT of the dedup — so the dedup no longer pre-filtered to live jobs and
-- the plan probed `jobs` for ~15k candidates (nested loop) to render 6. EXPLAIN:
-- the jobs Nested Loop dominated cold (~46k buffer touches).
--
-- Restructure so the dedup pre-filters to LIVE candidates (~3k) again, but
-- keeping the wins: the scores scan stays index-only (covering index), the live
-- `jobs` join is done ONCE inside the CTE (planner hash-joins the live set) and
-- CARRIES the jobs columns through, so nothing re-probes `jobs` per candidate,
-- and the heavy JSONB (score_breakdown, logistics_filters) is still fetched via
-- a LATERAL join for ONLY the final page rows.
--
-- Equivalent (a job is wholly live or wholly not, so live-filtering before the
-- dedup is the same as after) — guarded by the 66-case equivalence test. Same
-- signature/RETURNS ⇒ no API change. SECURITY INVOKER unchanged.

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
    v_dir     text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_gk      text;   -- graded sort key (b.* aliases; page + final share them)
    v_order   text;
    v_sql     text;
BEGIN
    IF p_recency_decay THEN
        v_gk := 'b.score * GREATEST(0.3, 1.0 - GREATEST(0.0, COALESCE(EXTRACT(EPOCH FROM (now() - b.first_seen_at)) / 86400.0, 0.0) - 7.0) * 0.015)';
    ELSE
        v_gk := 'b.score';
    END IF;

    IF p_sort = 'created_at' THEN
        v_order := format('b.created_at %1$s, b.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'company_name' THEN
        v_order := format('b.company_name %1$s, b.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'title' THEN
        v_order := format('b.title %1$s, b.job_posting_id %1$s', v_dir);
    ELSE
        v_order := format(
            'b.is_graded DESC, CASE WHEN b.is_graded THEN %1$s END %2$s, '
            || 'CASE WHEN NOT b.is_graded THEN COALESCE(b.first_seen_at, ''-infinity''::timestamptz) END %2$s, '
            || 'b.job_posting_id %2$s', v_gk, v_dir);
    END IF;

    -- `best` dedups to one row per LIVE job (live filter in-CTE keeps the
    -- candidate set small; jobs columns are carried so nothing re-probes jobs).
    -- The `page` filters family/status/company/search + sorts + limits over
    -- those carried columns. The final LATERAL fetches the heavy JSONB for the
    -- <=limit output rows only. `page`/final share the b.* aliases via a plain
    -- SELECT * so one order clause serves both.
    v_sql := format($q$
        WITH best AS (
            SELECT DISTINCT ON (s.job_posting_id)
                s.job_posting_id, s.target_id, s.score, s.scoring_status,
                (s.axis_scores IS NOT NULL) AS is_graded,
                jp.external_id, jp.source_id, jp.title, jp.company_name,
                jp.location, jp.department, jp.absolute_url, jp.salary_text,
                jp.greenhouse_updated_at, jp.first_seen_at, jp.created_at,
                jp.role_family
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
            ORDER BY s.job_posting_id, (s.axis_scores IS NOT NULL) DESC, s.score DESC
        ),
        page AS (
            SELECT b.*, COALESCE(uj.status, 'new')::text AS uj_status
            FROM best b
            INNER JOIN public.targets t ON t.id = b.target_id
            LEFT JOIN public.user_jobs uj
                ON uj.job_posting_id = b.job_posting_id AND uj.user_id = $1
            WHERE (t.role_family IS NULL OR b.role_family IS NULL OR b.role_family = t.role_family)
                AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
                AND ($5 IS NULL OR b.company_name = $5)
                AND ($6 IS NULL OR b.title ILIKE '%%' || $6 || '%%')
            ORDER BY %1$s
            LIMIT $7 OFFSET $8
        )
        SELECT
            b.job_posting_id AS id, b.external_id, b.source_id, b.title,
            b.company_name, b.location, b.department, b.absolute_url,
            b.score, s2.score_breakdown, b.scoring_status, s2.logistics_filters,
            b.uj_status AS status, b.salary_text, b.greenhouse_updated_at,
            b.first_seen_at, b.created_at, (NOT b.is_graded) AS pending
        FROM page b
        LEFT JOIN LATERAL (
            SELECT s2.score_breakdown, s2.logistics_filters
            FROM public.scores s2
            WHERE s2.job_posting_id = b.job_posting_id AND s2.target_id = b.target_id
            LIMIT 1
        ) s2 ON TRUE
        ORDER BY %1$s
    $q$, v_order);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset;
END;
$func$;
