-- Structured salary parts on jobs (query-grade columns).
--
-- ``salary_text`` is display-grade prose ("$190,800 [em-dash] $267,100 USD",
-- "$200K [en-dash] $400K", "$17.89 - $26.35 / Hour") — it cannot power range
-- filters, sorting, or aggregation. ``parse_salary_text``
-- (app/services/extract.py) derives (min, max, currency, period)
-- DETERMINISTICALLY from that already-extracted string at ingest — no LLM —
-- validated against every distinct salary_text prod has stored (5,834
-- formats). The raw text stays untouched as display + provenance, same model
-- as location/city/state/country (20260728000000).
--
-- ``salary_period`` is 'yearly' | 'hourly' | NULL (unknown — e.g. monthly
-- stipend ranges; filters only trust yearly). Amounts are numeric (hourly
-- rates carry cents).
--
-- Additive + backward-compatible: old code ignores the new columns; the two
-- list RPCs are dropped/recreated (RETURNS TABLE changes require it) inside
-- this transaction with the same bodies plus the four pass-through columns.

ALTER TABLE public.jobs
  ADD COLUMN IF NOT EXISTS salary_min numeric,
  ADD COLUMN IF NOT EXISTS salary_max numeric,
  ADD COLUMN IF NOT EXISTS salary_currency text,
  ADD COLUMN IF NOT EXISTS salary_period text;

-- ---------------------------------------------------------------------------
-- get_target_jobs: verbatim 20260728000000 definition + the four salary columns.
-- ---------------------------------------------------------------------------

DROP FUNCTION IF EXISTS public.get_target_jobs(
  uuid, integer, text, text, text, text, boolean, integer, text, uuid, uuid);

CREATE OR REPLACE FUNCTION public.get_target_jobs(p_target_id uuid, p_min_score integer DEFAULT 0, p_status text DEFAULT NULL::text, p_company text DEFAULT NULL::text, p_search text DEFAULT NULL::text, p_sort text DEFAULT 'score'::text, p_ascending boolean DEFAULT false, p_limit integer DEFAULT 20, p_after_value text DEFAULT NULL::text, p_after_id uuid DEFAULT NULL::uuid, p_user_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id uuid, external_id text, source_id uuid, title text, company_name text, location text, city text, state text, country text, location_remote boolean, department text, absolute_url text, score integer, score_breakdown jsonb, scoring_status text, logistics_filters jsonb, status text, salary_text text, salary_min numeric, salary_max numeric, salary_currency text, salary_period text, greenhouse_updated_at timestamp with time zone, first_seen_at timestamp with time zone, created_at timestamp with time zone)
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
    -- The target's role family (NULL = unclassified → the gate below is a no-op).
    -- Alias the table: ``id`` is also an OUT-param name, so an unqualified
    -- ``WHERE id = …`` is ambiguous (PL/pgSQL variable vs column).
    SELECT t.role_family INTO v_family FROM public.targets AS t WHERE t.id = p_target_id;

    -- Whitelist the sort column + its cast (never interpolate user input).
    CASE p_sort
        WHEN 'created_at'   THEN v_col := 'jp.created_at';   v_cast := 'timestamptz';
        WHEN 'company_name' THEN v_col := 'jp.company_name'; v_cast := 'text';
        WHEN 'title'        THEN v_col := 'jp.title';        v_cast := 'text';
        ELSE                     v_col := 's.score';         v_cast := 'integer';
    END CASE;

    v_sql := format($q$
        SELECT
            jp.id, jp.external_id, jp.source_id, jp.title,
            jp.company_name, jp.location,
            jp.city, jp.state, jp.country, jp.location_remote,
            jp.department,
            jp.absolute_url, s.score, s.score_breakdown,
            s.scoring_status, s.logistics_filters,
            COALESCE(uj.status, 'new')::text AS status,
            jp.salary_text, jp.salary_min, jp.salary_max,
            jp.salary_currency, jp.salary_period,
            jp.greenhouse_updated_at, jp.first_seen_at,
            jp.created_at
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

-- ---------------------------------------------------------------------------
-- get_cross_target_jobs: verbatim 20260728000000 definition + the four salary columns.
-- ---------------------------------------------------------------------------

DROP FUNCTION IF EXISTS public.get_cross_target_jobs(
  uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean, jsonb);

CREATE OR REPLACE FUNCTION public.get_cross_target_jobs(p_target_ids uuid[], p_min_score integer DEFAULT 0, p_status text DEFAULT NULL::text, p_company text DEFAULT NULL::text, p_search text DEFAULT NULL::text, p_sort text DEFAULT 'score'::text, p_ascending boolean DEFAULT false, p_limit integer DEFAULT 20, p_offset integer DEFAULT 0, p_user_id uuid DEFAULT NULL::uuid, p_recency_decay boolean DEFAULT false, p_weights jsonb DEFAULT '{}'::jsonb)
 RETURNS TABLE(id uuid, external_id text, source_id uuid, title text, company_name text, location text, city text, state text, country text, location_remote boolean, department text, absolute_url text, score integer, raw_score integer, score_breakdown jsonb, scoring_status text, logistics_filters jsonb, status text, salary_text text, salary_min numeric, salary_max numeric, salary_currency text, salary_period text, greenhouse_updated_at timestamp with time zone, first_seen_at timestamp with time zone, created_at timestamp with time zone, pending boolean)
 LANGUAGE plpgsql
 STABLE
 SET search_path TO ''
AS $function$
DECLARE
    v_dir        text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    -- Custom weights present for at least one target => compute the blend.
    v_need_axis  boolean := (p_weights IS NOT NULL AND p_weights <> '{}'::jsonb);
    v_disp       text;   -- undecayed display value expression (blend or raw)
    v_axis_col   text;   -- extra best-CTE column (axis_scores only when weighted)
    v_gk         text;   -- graded sort key (b.* aliases; page + final share them)
    v_order      text;
    v_page_jobs  boolean;  -- does the page CTE need a jobs join?
    v_page_join  text;
    v_page_filt  text;
    v_sql        text;
BEGIN
    -- The displayed score. Weighted blend when any target carries custom weights
    -- (parity: wyrdfold_display_score, which mirrors display_score_from_axes);
    -- otherwise bare b.score — identical to the pre-#457 query, index-only.
    IF v_need_axis THEN
        v_disp     := 'public.wyrdfold_display_score(b.axis_scores, b.score, $9 -> b.target_id::text)';
        v_axis_col := ', s.axis_scores';
    ELSE
        v_disp     := 'b.score';
        v_axis_col := '';
    END IF;

    -- Graded key: the display value, or age-decayed when decay is on (prod).
    -- Uses the denormalized (immutable) first_seen; NULL ⇒ age 0.
    IF p_recency_decay THEN
        v_gk := v_disp || ' * GREATEST(0.3, 1.0 - GREATEST(0.0, COALESCE(EXTRACT(EPOCH FROM (now() - b.job_first_seen_at)) / 86400.0, 0.0) - 7.0) * 0.015)';
    ELSE
        v_gk := v_disp;
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
            jp.department, jp.absolute_url,
            %5$s AS score, b.score AS raw_score, s2.score_breakdown,
            b.scoring_status, s2.logistics_filters,
            b.uj_status AS status, jp.salary_text, jp.salary_min, jp.salary_max,
            jp.salary_currency, jp.salary_period, jp.greenhouse_updated_at,
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
    $q$, v_order, v_page_join, v_page_filt, v_axis_col, v_disp);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset, p_weights;
END;
$function$;

-- ---------------------------------------------------------------------------
-- bulk_update_salaries: same signature, now also writes the structured parts
-- carried in each update object (the admin backfill route computes them
-- alongside the text). Absent parts keys set the columns NULL — acceptable:
-- the route is the only caller and always sends all five fields.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.bulk_update_salaries(p_updates jsonb)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_catalog'
AS $function$
DECLARE
  cnt integer;
BEGIN
  IF p_updates IS NULL OR jsonb_array_length(p_updates) = 0 THEN
    RETURN 0;
  END IF;

  WITH u AS (
    SELECT (elem->>'id')::uuid          AS id,
           (elem->>'salary_text')       AS salary_text,
           (elem->>'salary_min')::numeric  AS salary_min,
           (elem->>'salary_max')::numeric  AS salary_max,
           (elem->>'salary_currency')   AS salary_currency,
           (elem->>'salary_period')     AS salary_period
    FROM   jsonb_array_elements(p_updates) elem
  )
  UPDATE public.jobs jp
  SET    salary_text     = u.salary_text,
         salary_min      = u.salary_min,
         salary_max      = u.salary_max,
         salary_currency = u.salary_currency,
         salary_period   = u.salary_period
  FROM   u
  WHERE  jp.id = u.id;

  GET DIAGNOSTICS cnt = ROW_COUNT;
  RETURN cnt;
END;
$function$;
