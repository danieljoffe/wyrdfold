-- #457 — teach get_cross_target_jobs to sort by the weighted display score.
--
-- Score-sorted /jobs lists rank by the score each row DISPLAYS: the per-(user,
-- target) axis-weighted blend of Phase 2's axis_scores. Because that blend is
-- per-user it can't be a stored column, so any user with custom axis weights
-- fell OFF the fast RPC (get_cross_target_jobs / get_target_jobs) onto the
-- Python two-query path — which fetches EVERY candidate scores row (JSONB and
-- all) for the target(s) and ranks in Python. On the small instance that's the
-- 2.7-9s /jobs family measured in the 2026-07-23 sweep (owner's one active
-- custom-weight target: 1,597 candidate rows pulled to rank ~20).
--
-- The blend is a plain weighted average, so it IS SQL-expressible. This migration
-- computes it DB-side inside the RPC so custom-weight users use the same single-
-- round-trip fast path as everyone else — no fetch-all, no approximation, exact
-- parity with the Python display order.

BEGIN;

-- ---------------------------------------------------------------------------
-- The reusable display-score primitive. Mirrors
-- app/services/fit/axis_weights.py::display_score_from_axes /
-- display_score_or_passthrough EXACTLY — keep the two in lockstep (the
-- differential parity test tests/integration/test_cross_target_jobs_rpc.py
-- pins RPC-score == Python-blend across a battery).
--
-- Parity notes:
--   * float8 arithmetic + round(double precision) reproduces Python's float
--     math + round()'s banker's rounding (round-half-to-even). round(numeric)
--     would round half AWAY from zero and diverge on .5 ties (e.g. 70.5).
--   * Passthrough: NULL weights OR no axis payload -> the raw Sonnet score
--     (display_score_or_passthrough: "weights is None or not axes").
--   * total_w <= 0 (user zeroed every axis) -> 0, guarding the divide, matching
--     display_score_from_axes.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.wyrdfold_display_score(
    p_axis_scores jsonb,
    p_raw_score   integer,
    p_weights     jsonb
) RETURNS integer
LANGUAGE sql
IMMUTABLE
SET search_path TO ''
AS $$
    SELECT CASE
        WHEN p_weights IS NULL
             OR p_axis_scores IS NULL
             OR p_axis_scores = '{}'::jsonb
            THEN p_raw_score
        WHEN COALESCE((p_weights->>'title_fit')::float8, 0)
           + COALESCE((p_weights->>'skills_fit')::float8, 0)
           + COALESCE((p_weights->>'seniority_fit')::float8, 0)
           + COALESCE((p_weights->>'domain_fit')::float8, 0) <= 0
            THEN 0
        ELSE round(
            ( COALESCE((p_axis_scores->>'title_fit')::float8, 0)     * COALESCE((p_weights->>'title_fit')::float8, 0)
            + COALESCE((p_axis_scores->>'skills_fit')::float8, 0)    * COALESCE((p_weights->>'skills_fit')::float8, 0)
            + COALESCE((p_axis_scores->>'seniority_fit')::float8, 0) * COALESCE((p_weights->>'seniority_fit')::float8, 0)
            + COALESCE((p_axis_scores->>'domain_fit')::float8, 0)    * COALESCE((p_weights->>'domain_fit')::float8, 0) )
            / ( COALESCE((p_weights->>'title_fit')::float8, 0)
              + COALESCE((p_weights->>'skills_fit')::float8, 0)
              + COALESCE((p_weights->>'seniority_fit')::float8, 0)
              + COALESCE((p_weights->>'domain_fit')::float8, 0) )
        )::integer
    END
$$;

COMMENT ON FUNCTION public.wyrdfold_display_score(jsonb, integer, jsonb) IS
    'Weighted axis blend for /jobs display+sort. Mirrors '
    'app/services/fit/axis_weights.py::display_score_from_axes (float8 + '
    'banker''s rounding); passthrough to raw score when weights/axes absent. #457.';

-- ---------------------------------------------------------------------------
-- get_cross_target_jobs gains p_weights: a JSONB map target_id -> axis-weight
-- object ({"title_fit":..,"skills_fit":..,"seniority_fit":..,"domain_fit":..}).
-- When non-empty, the graded sort key + the returned ``score`` become the
-- weighted blend (age-decayed for the sort when p_recency_decay); raw_score
-- carries the undecayed Sonnet score so _apply_display_recency keeps it intact.
--
-- When p_weights is empty ('{}', the default) the built SQL is byte-for-byte
-- today's: bare ``b.score`` sort key, no axis_scores column carried, index-only
-- scan preserved — the common (no-custom-weights) path is untouched. Only the
-- weighted path carries axis_scores (a heap fetch) and calls the blend fn.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_cross_target_jobs(
    p_target_ids uuid[],
    p_min_score integer DEFAULT 0,
    p_status text DEFAULT NULL::text,
    p_company text DEFAULT NULL::text,
    p_search text DEFAULT NULL::text,
    p_sort text DEFAULT 'score'::text,
    p_ascending boolean DEFAULT false,
    p_limit integer DEFAULT 20,
    p_offset integer DEFAULT 0,
    p_user_id uuid DEFAULT NULL::uuid,
    p_recency_decay boolean DEFAULT false,
    p_weights jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
    id uuid, external_id text, source_id uuid, title text, company_name text,
    location text, department text, absolute_url text,
    score integer, raw_score integer, score_breakdown jsonb, scoring_status text,
    logistics_filters jsonb, status text, salary_text text,
    greenhouse_updated_at timestamp with time zone,
    first_seen_at timestamp with time zone, created_at timestamp with time zone,
    pending boolean
)
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
            jp.company_name, jp.location, jp.department, jp.absolute_url,
            %5$s AS score, b.score AS raw_score, s2.score_breakdown,
            b.scoring_status, s2.logistics_filters,
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
    $q$, v_order, v_page_join, v_page_filt, v_axis_col, v_disp);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset, p_weights;
END;
$function$;

COMMIT;
