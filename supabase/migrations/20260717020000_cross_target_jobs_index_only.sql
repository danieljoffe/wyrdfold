-- perf(#365 follow-up): stop reading thousands of rows to render six.
--
-- Rewrite get_cross_target_jobs so it reads only what it needs. Same signature,
-- same RETURNS shape, SAME OUTPUT (guarded by the 66-case equivalence test) —
-- only the plan changes:
--
--   1. `best`: dedup the cross-target scored set INDEX-ONLY (the covering index
--      from 20260717010000 carries every column: target_id/excluded/score/
--      job_posting_id/scoring_status/axis_scores). No jobs join here anymore.
--      is_graded simplifies to `axis_scores IS NOT NULL` — prod has zero empty/
--      non-object axis_scores, so it equals the old jsonb_typeof check but skips
--      a per-row jsonb op. The live/family/status filters move OUT of the dedup.
--   2. `page`: join jobs ONLY on the ~3k deduped candidates, apply the live gate
--      (archived/purged/is_us — a per-JOB property, so applying it after the
--      dedup is equivalent: a job is wholly live or wholly not), the off-family
--      gate, status/company/search, then ORDER BY + LIMIT. This replaces the old
--      full ~5k-live-jobs hash scan with candidate lookups.
--   3. Fetch the heavy JSONB (score_breakdown, logistics_filters) via a LATERAL
--      join for ONLY the final page rows, not every candidate.
--
-- Net: the cold ~7.6s / ~9,400-page read collapses to an index-only dedup + a
-- few-thousand candidate lookups + JSONB for the page. SECURITY INVOKER
-- unchanged (RLS governs); CREATE OR REPLACE preserves grants.

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
    v_gk_in   text;  -- graded sort key, page-CTE aliases (b.score / jp.first_seen_at)
    v_gk_out  text;  -- graded sort key, final aliases (p.score / p.first_seen_at)
    v_order   text;  -- page CTE order
    v_order_f text;  -- final order (re-sorts the LIMITed page after the LATERAL)
    v_sql     text;
BEGIN
    -- Graded key: raw score, or the age-decayed score when decay is on (prod).
    -- Multiplier = compute_recency_multiplier(age); NULL first_seen ⇒ age 0.
    IF p_recency_decay THEN
        v_gk_in  := 'b.score * GREATEST(0.3, 1.0 - GREATEST(0.0, COALESCE(EXTRACT(EPOCH FROM (now() - jp.first_seen_at)) / 86400.0, 0.0) - 7.0) * 0.015)';
        v_gk_out := 'p.score * GREATEST(0.3, 1.0 - GREATEST(0.0, COALESCE(EXTRACT(EPOCH FROM (now() - p.first_seen_at))  / 86400.0, 0.0) - 7.0) * 0.015)';
    ELSE
        v_gk_in  := 'b.score';
        v_gk_out := 'p.score';
    END IF;

    -- Order clauses. Score sort mirrors _rank_graded_first (graded first, graded
    -- by (decayed) score, Pending by first_seen recency); other sorts a plain
    -- column order. v_dir is whitelisted; no user input is interpolated.
    IF p_sort = 'created_at' THEN
        v_order   := format('jp.created_at %1$s, b.job_posting_id %1$s', v_dir);
        v_order_f := format('p.created_at %1$s, p.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'company_name' THEN
        v_order   := format('jp.company_name %1$s, b.job_posting_id %1$s', v_dir);
        v_order_f := format('p.company_name %1$s, p.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'title' THEN
        v_order   := format('jp.title %1$s, b.job_posting_id %1$s', v_dir);
        v_order_f := format('p.title %1$s, p.job_posting_id %1$s', v_dir);
    ELSE
        v_order := format(
            'b.is_graded DESC, CASE WHEN b.is_graded THEN %1$s END %2$s, '
            || 'CASE WHEN NOT b.is_graded THEN COALESCE(jp.first_seen_at, ''-infinity''::timestamptz) END %2$s, '
            || 'b.job_posting_id %2$s', v_gk_in, v_dir);
        v_order_f := format(
            'p.is_graded DESC, CASE WHEN p.is_graded THEN %1$s END %2$s, '
            || 'CASE WHEN NOT p.is_graded THEN COALESCE(p.first_seen_at, ''-infinity''::timestamptz) END %2$s, '
            || 'p.job_posting_id %2$s', v_gk_out, v_dir);
    END IF;

    v_sql := format($q$
        WITH best AS (
            SELECT DISTINCT ON (s.job_posting_id)
                s.job_posting_id, s.target_id, s.score, s.scoring_status,
                (s.axis_scores IS NOT NULL) AS is_graded
            FROM public.scores s
            WHERE s.target_id = ANY ($2)
                AND s.excluded = FALSE
                AND ($3 IS NULL
                     OR s.scoring_status IS DISTINCT FROM 'complete'
                     OR s.score >= $3)
            ORDER BY s.job_posting_id, (s.axis_scores IS NOT NULL) DESC, s.score DESC
        ),
        page AS (
            SELECT
                jp.id, jp.external_id, jp.source_id, jp.title,
                jp.company_name, jp.location, jp.department, jp.absolute_url,
                b.score, b.scoring_status,
                COALESCE(uj.status, 'new')::text AS status,
                jp.salary_text, jp.greenhouse_updated_at, jp.first_seen_at,
                jp.created_at, (NOT b.is_graded) AS pending,
                b.job_posting_id, b.target_id, b.is_graded
            FROM best b
            INNER JOIN public.jobs jp ON jp.id = b.job_posting_id
            INNER JOIN public.targets t ON t.id = b.target_id
            LEFT JOIN public.user_jobs uj
                ON uj.job_posting_id = jp.id AND uj.user_id = $1
            WHERE jp.archived_at IS NULL
                AND jp.purged_at IS NULL
                AND jp.is_us IS NOT FALSE
                AND (t.role_family IS NULL OR jp.role_family IS NULL OR jp.role_family = t.role_family)
                AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
                AND ($5 IS NULL OR jp.company_name = $5)
                AND ($6 IS NULL OR jp.title ILIKE '%%' || $6 || '%%')
            ORDER BY %1$s
            LIMIT $7 OFFSET $8
        )
        SELECT
            p.id, p.external_id, p.source_id, p.title,
            p.company_name, p.location, p.department, p.absolute_url,
            p.score, s2.score_breakdown, p.scoring_status, s2.logistics_filters,
            p.status, p.salary_text, p.greenhouse_updated_at, p.first_seen_at,
            p.created_at, p.pending
        FROM page p
        LEFT JOIN LATERAL (
            SELECT s2.score_breakdown, s2.logistics_filters
            FROM public.scores s2
            WHERE s2.job_posting_id = p.job_posting_id AND s2.target_id = p.target_id
            LIMIT 1
        ) s2 ON TRUE
        ORDER BY %2$s
    $q$, v_order, v_order_f);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset;
END;
$func$;
