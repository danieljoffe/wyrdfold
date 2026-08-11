-- Post-R2 follow-ups (schema audit open forks, decided 2026-07-31):
--
-- 1. URL-health cadence fix ("fix the net", docs/decisions.md): the archival
--    cascade has NEVER fired in prod — NULLS-FIRST due-ordering meant a
--    failing URL's second strike waited behind the entire never-checked
--    backlog (~82 days/strike at batch 50 vs a 30-day retention horizon).
--    New ordering: rows already carrying strikes FIRST (confirm dying URLs on
--    consecutive ticks → archival in ~threshold days), then never-checked
--    (discovery), then stale re-checks. Paired with the batch-size default
--    bump in config (50 → 250).
--
-- 2. Drop jobs.department + jobs.us_confidence (Group B, owner-decided).
--    guarded-destructive: department — provider metadata with ZERO consumers
--    ever (returned everywhere, rendered/used nowhere; audit Group B) and
--    re-obtainable from the source ATS APIs; us_confidence — consumed once
--    in-flight at tag time (the archive-non-US decision uses the tagger's
--    in-memory value), never read back from the column by anything.
--
-- 3. Both list RPCs re-issued minus the department output column
--    (DROP+CREATE — return shape changes).

-- ---- 1. URL-health due-ordering ---------------------------------------------
CREATE OR REPLACE FUNCTION public.due_url_health_jobs(p_cutoff timestamp with time zone, p_batch_size integer)
 RETURNS TABLE(id uuid, absolute_url text, url_check_failure_count integer)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public', 'pg_catalog'
AS $function$
  SELECT j.id, j.absolute_url, j.url_check_failure_count
  FROM public.jobs j
  WHERE j.archived_at IS NULL
    AND (j.last_url_check_at IS NULL OR j.last_url_check_at <= p_cutoff)
    AND NOT EXISTS (SELECT 1 FROM public.user_jobs uj
                    WHERE uj.job_posting_id = j.id AND uj.status <> 'new')
  -- Failure-first (2026-07-31 cadence fix): a row with strikes re-checks on
  -- the very next due tick, so the 3-strike archival completes in days
  -- instead of waiting behind the never-checked backlog. Then never-checked
  -- (discovery), then stalest re-checks.
  ORDER BY (j.url_check_failure_count > 0) DESC,
           j.last_url_check_at ASC NULLS FIRST
  LIMIT p_batch_size;
$function$;

-- ---- 2. Column drops ---------------------------------------------------------
ALTER TABLE public.jobs DROP COLUMN IF EXISTS department;
ALTER TABLE public.jobs DROP COLUMN IF EXISTS us_confidence;

-- ---- 3. List RPCs minus department -------------------------------------------
DROP FUNCTION IF EXISTS public.get_target_jobs(uuid, integer, text, text, text, text, boolean, integer, text, uuid, uuid);
DROP FUNCTION IF EXISTS public.get_cross_target_jobs(uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean, jsonb);

CREATE FUNCTION public.get_target_jobs(p_target_id uuid, p_min_score integer DEFAULT 0, p_status text DEFAULT NULL::text, p_company text DEFAULT NULL::text, p_search text DEFAULT NULL::text, p_sort text DEFAULT 'score'::text, p_ascending boolean DEFAULT false, p_limit integer DEFAULT 20, p_after_value text DEFAULT NULL::text, p_after_id uuid DEFAULT NULL::uuid, p_user_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id uuid, external_id text, source_id uuid, title text, company_name text, location text, city text, state text, country text, location_remote boolean, employment_type text, seniority text, metro text, is_remote boolean, absolute_url text, score integer, score_breakdown jsonb, scoring_status text, logistics_filters jsonb, status text, salary_text text, salary_min numeric, salary_max numeric, salary_currency text, salary_period text, source_posted_at timestamp with time zone, cataloged_at timestamp with time zone)
 LANGUAGE plpgsql
 STABLE
 SET search_path TO ''
AS $function$
DECLARE
    v_col    text;
    v_cast   text;
    v_dir    text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_cmp    text := CASE WHEN p_ascending THEN '>'  ELSE '<'   END;
    v_sql    text;
    v_family text;
BEGIN
    SELECT t.role_family INTO v_family FROM public.targets AS t WHERE t.id = p_target_id;

    CASE p_sort
        WHEN 'created_at'   THEN v_col := 'jp.cataloged_at';  v_cast := 'timestamptz';
        WHEN 'company_name' THEN v_col := 'jp.company_name';  v_cast := 'text';
        WHEN 'title'        THEN v_col := 'jp.title';         v_cast := 'text';
        ELSE                     v_col := 's.score';          v_cast := 'integer';
    END CASE;

    v_sql := format($q$
        SELECT
            jp.id, jp.external_id, jp.source_id, jp.title,
            jp.company_name, jp.location,
            jp.city, jp.state, jp.country, jp.location_remote,
            jp.employment_type, jp.seniority, jp.metro, jp.is_remote,
            jp.absolute_url, s.score, s.score_breakdown,
            s.scoring_status, s.logistics_filters,
            COALESCE(uj.status, 'new')::text AS status,
            jp.salary_text, jp.salary_min, jp.salary_max,
            jp.salary_currency, jp.salary_period,
            jp.source_posted_at, jp.cataloged_at
        FROM public.scores s
        INNER JOIN public.jobs jp ON jp.id = s.job_posting_id
        LEFT JOIN public.user_jobs uj
            ON uj.job_posting_id = jp.id AND uj.user_id = $1
        WHERE s.target_id = $2
            AND s.excluded = FALSE
            AND s.score >= $3
            AND jp.archived_at IS NULL
            AND jp.is_us IS NOT FALSE
            AND ($10 IS NULL OR jp.role_family IS NULL OR jp.role_family = $10)
            AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
            AND ($5 IS NULL OR jp.company_name = $5)
            AND ($6 IS NULL OR jp.title ILIKE '%%' || $6 || '%%')
            AND ($7 IS NULL OR (%1$s, s.job_posting_id) %2$s ($7::%3$s, $8))
        ORDER BY %1$s %4$s, s.job_posting_id %4$s
        LIMIT $9
    $q$, v_col, v_cmp, v_cast, v_dir);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_id, p_min_score, p_status, p_company,
              p_search, p_after_value, p_after_id, p_limit, v_family;
END;
$function$;

CREATE FUNCTION public.get_cross_target_jobs(p_target_ids uuid[], p_min_score integer DEFAULT 0, p_status text DEFAULT NULL::text, p_company text DEFAULT NULL::text, p_search text DEFAULT NULL::text, p_sort text DEFAULT 'score'::text, p_ascending boolean DEFAULT false, p_limit integer DEFAULT 20, p_offset integer DEFAULT 0, p_user_id uuid DEFAULT NULL::uuid, p_recency_decay boolean DEFAULT false, p_weights jsonb DEFAULT '{}'::jsonb)
 RETURNS TABLE(id uuid, external_id text, source_id uuid, title text, company_name text, location text, city text, state text, country text, location_remote boolean, employment_type text, seniority text, metro text, is_remote boolean, absolute_url text, score integer, raw_score integer, score_breakdown jsonb, scoring_status text, logistics_filters jsonb, status text, salary_text text, salary_min numeric, salary_max numeric, salary_currency text, salary_period text, source_posted_at timestamp with time zone, cataloged_at timestamp with time zone, pending boolean)
 LANGUAGE plpgsql
 STABLE
 SET search_path TO ''
AS $function$
DECLARE
    v_dir        text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_need_axis  boolean := (p_weights IS NOT NULL AND p_weights <> '{}'::jsonb);
    v_disp       text;
    v_axis_col   text;
    v_gk         text;
    v_order      text;
    v_page_jobs  boolean;
    v_page_join  text;
    v_page_filt  text;
    v_sql        text;
BEGIN
    IF v_need_axis THEN
        v_disp     := 'public.wyrdfold_display_score(b.axis_scores, b.score, $9 -> b.target_id::text)';
        v_axis_col := ', s.axis_scores';
    ELSE
        v_disp     := 'b.score';
        v_axis_col := '';
    END IF;

    IF p_recency_decay THEN
        v_gk := v_disp || ' * GREATEST(0.3, 1.0 - GREATEST(0.0, COALESCE(EXTRACT(EPOCH FROM (now() - b.job_first_seen_at)) / 86400.0, 0.0) - 7.0) * 0.015)';
    ELSE
        v_gk := v_disp;
    END IF;

    v_page_jobs := (p_sort IN ('created_at', 'company_name', 'title'))
                   OR (p_company IS NOT NULL)
                   OR (p_search IS NOT NULL);

    IF p_sort = 'created_at' THEN
        v_order := format('jp.cataloged_at %1$s, b.job_posting_id %1$s', v_dir);
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
        v_page_filt := 'AND ($5 IS NULL OR jp.company_name = $5) '
                    || 'AND ($6 IS NULL OR jp.title ILIKE ''%'' || $6 || ''%'')';
    ELSE
        v_page_join := '';
        v_page_filt := 'AND ($5 IS NULL) AND ($6 IS NULL)';
    END IF;

    v_sql := format($q$
        WITH tgt AS MATERIALIZED (
            SELECT id, role_family FROM public.targets WHERE id = ANY ($2)
        ),
        uj_user AS MATERIALIZED (
            SELECT job_posting_id, status FROM public.user_jobs WHERE user_id = $1
        ),
        best AS (
            SELECT DISTINCT ON (s.job_posting_id)
                s.job_posting_id, s.target_id, s.score, s.scoring_status,
                s.is_graded, s.job_role_family, s.job_first_seen_at%4$s
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
            jp.company_name, jp.location,
            jp.city, jp.state, jp.country, jp.location_remote,
            jp.employment_type, jp.seniority, jp.metro, jp.is_remote,
            jp.absolute_url,
            %5$s AS score, b.score AS raw_score, s2.score_breakdown,
            b.scoring_status, s2.logistics_filters,
            b.uj_status AS status, jp.salary_text, jp.salary_min, jp.salary_max,
            jp.salary_currency, jp.salary_period, jp.source_posted_at,
            jp.cataloged_at, (NOT b.is_graded) AS pending
        FROM page b
        INNER JOIN public.jobs jp ON jp.id = b.job_posting_id
        LEFT JOIN LATERAL (
            SELECT s2.score_breakdown, s2.logistics_filters
            FROM public.scores s2
            WHERE s2.job_posting_id = b.job_posting_id AND s2.target_id = b.target_id
            LIMIT 1
        ) s2 ON TRUE
        ORDER BY %1$s
    $q$, v_order, v_page_join, v_page_filt, v_axis_col, v_disp);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset, p_weights;
END;
$function$;

GRANT EXECUTE ON FUNCTION public.get_target_jobs(uuid, integer, text, text, text, text, boolean, integer, text, uuid, uuid) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_cross_target_jobs(uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean, jsonb) TO anon, authenticated, service_role;
