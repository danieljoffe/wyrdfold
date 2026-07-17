-- perf(#365 floor): rewrite get_cross_target_jobs to use the denormalized scores
-- columns (20260717040000) so the common dashboard page is INDEX-ONLY.
--
-- Same signature, same RETURNS, SAME OUTPUT (guarded by the equivalence test) —
-- only the plan changes:
--
--   * `best` dedups the cross-target scored set from the PARTIAL covering index
--     alone (idx_scores_live_dedup: live+non-excluded rows, carrying is_graded /
--     job_role_family / job_first_seen_at, no JSONB). No jobs join, no scores
--     heap. The live filter is the index predicate; gradedness/family/first_seen
--     come from the denormalized columns.
--   * `page` applies the off-family gate (denormalized job_role_family vs the
--     winning target), the per-user status filter, and the sort/limit over those
--     carried columns. For the common case (score sort, no company/search) this
--     needs NO jobs read. It joins jobs ONLY when the sort key or a filter needs
--     a non-denormalized column (title / company_name / created_at sort, or a
--     company/search filter) — the rarer paths.
--   * the final SELECT joins jobs once for the <=limit output rows (display) and
--     LATERAL-fetches the heavy JSONB (score_breakdown, logistics_filters) for
--     those rows only.
--
-- Net: the ~18,900-page working set collapses to an index-only dedup of ~3.3k
-- live rows + <=limit job lookups. Equivalent because job_is_live/role_family/
-- first_seen are per-JOB (denormalized, trigger-synced) and first_seen is
-- immutable, so the sort key can't drift from the displayed value. SECURITY
-- INVOKER unchanged; CREATE OR REPLACE preserves grants.

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
    v_gk         text;   -- graded sort key (b.* aliases; page + final share them)
    v_order      text;
    v_page_jobs  boolean;  -- does the page CTE need a jobs join?
    v_page_join  text;
    v_page_filt  text;
    v_sql        text;
BEGIN
    -- Graded key: raw score, or the age-decayed score when decay is on (prod).
    -- Uses the denormalized (immutable) first_seen; NULL ⇒ age 0.
    IF p_recency_decay THEN
        v_gk := 'b.score * GREATEST(0.3, 1.0 - GREATEST(0.0, COALESCE(EXTRACT(EPOCH FROM (now() - b.job_first_seen_at)) / 86400.0, 0.0) - 7.0) * 0.015)';
    ELSE
        v_gk := 'b.score';
    END IF;

    -- The page joins jobs only when the sort key or a filter needs a
    -- non-denormalized column. Score sort with no company/search stays index-only.
    v_page_jobs := (p_sort IN ('created_at', 'company_name', 'title'))
                   OR (p_company IS NOT NULL)
                   OR (p_search IS NOT NULL);

    -- One order clause serves both the page CTE and the final SELECT: b.* are the
    -- carried scores columns (present in both), jp is the jobs join (present in
    -- the page only when v_page_jobs, and always in the final). Score sort never
    -- references jp; the column sorts do, and are only taken when jobs is joined.
    IF p_sort = 'created_at' THEN
        v_order := format('jp.created_at %1$s, b.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'company_name' THEN
        v_order := format('jp.company_name %1$s, b.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'title' THEN
        v_order := format('jp.title %1$s, b.job_posting_id %1$s', v_dir);
    ELSE
        v_order := format(
            'b.is_graded DESC, CASE WHEN b.is_graded THEN %1$s END %2$s, '
            || 'CASE WHEN NOT b.is_graded THEN COALESCE(b.job_first_seen_at, ''-infinity''::timestamptz) END %2$s, '
            || 'b.job_posting_id %2$s', v_gk, v_dir);
    END IF;

    IF v_page_jobs THEN
        v_page_join := 'INNER JOIN public.jobs jp ON jp.id = b.job_posting_id';
        -- Interpolated via %3$s (a format ARG, not the template), so these
        -- wildcards are single '%' — not the '%%' the template itself would need.
        v_page_filt := 'AND ($5 IS NULL OR jp.company_name = $5) '
                    || 'AND ($6 IS NULL OR jp.title ILIKE ''%'' || $6 || ''%'')';
    ELSE
        v_page_join := '';
        -- Keep $5/$6 referenced (they are NULL on this path) so the USING list
        -- stays positionally aligned regardless of which params the plan uses.
        v_page_filt := 'AND ($5 IS NULL) AND ($6 IS NULL)';
    END IF;

    v_sql := format($q$
        WITH tgt AS MATERIALIZED (
            -- The user's few targets, fetched ONCE. Without MATERIALIZED the
            -- planner nested-loops a targets PK lookup per candidate (the DISTINCT
            -- ON row estimate is ~1, so it thinks the loop is cheap) — re-reading
            -- the same handful of targets thousands of times.
            SELECT id, role_family FROM public.targets WHERE id = ANY ($2)
        ),
        uj_user AS MATERIALIZED (
            -- Same reasoning for the per-user status rows: the user's user_jobs
            -- fetched ONCE (they have at most a few) and hash-joined, instead of a
            -- user_jobs index probe per candidate over the full deduped set.
            SELECT job_posting_id, status FROM public.user_jobs WHERE user_id = $1
        ),
        best AS (
            SELECT DISTINCT ON (s.job_posting_id)
                s.job_posting_id, s.target_id, s.score, s.scoring_status,
                s.is_graded, s.job_role_family, s.job_first_seen_at
            FROM public.scores s
            WHERE s.target_id = ANY ($2)
                AND s.job_is_live
                AND s.excluded = FALSE
                AND ($3 IS NULL
                     OR s.scoring_status IS DISTINCT FROM 'complete'
                     OR s.score >= $3)
            ORDER BY s.job_posting_id, s.is_graded DESC, s.score DESC
        ),
        page AS (
            SELECT b.*, COALESCE(uj.status, 'new')::text AS uj_status
            FROM best b
            INNER JOIN tgt t ON t.id = b.target_id
            %2$s
            LEFT JOIN uj_user uj
                ON uj.job_posting_id = b.job_posting_id
            WHERE (t.role_family IS NULL OR b.job_role_family IS NULL OR b.job_role_family = t.role_family)
                AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
                %3$s
            ORDER BY %1$s
            LIMIT $7 OFFSET $8
        )
        SELECT
            b.job_posting_id AS id, jp.external_id, jp.source_id, jp.title,
            jp.company_name, jp.location, jp.department, jp.absolute_url,
            b.score, s2.score_breakdown, b.scoring_status, s2.logistics_filters,
            b.uj_status AS status, jp.salary_text, jp.greenhouse_updated_at,
            jp.first_seen_at, jp.created_at, (NOT b.is_graded) AS pending
        FROM page b
        INNER JOIN public.jobs jp ON jp.id = b.job_posting_id
        LEFT JOIN LATERAL (
            SELECT s2.score_breakdown, s2.logistics_filters
            FROM public.scores s2
            WHERE s2.job_posting_id = b.job_posting_id AND s2.target_id = b.target_id
            LIMIT 1
        ) s2 ON TRUE
        ORDER BY %1$s
    $q$, v_order, v_page_join, v_page_filt);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset;
END;
$func$;
