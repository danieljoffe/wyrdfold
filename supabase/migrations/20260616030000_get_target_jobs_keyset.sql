-- #113: keyset (cursor) pagination for get_target_jobs.
--
-- The prior body ordered by a CASE-per-sort expression (not index-supportable)
-- and paginated with deep OFFSET plus a COUNT(*) OVER () window — both degrade
-- as a target's scores grow. This rewrites it to keyset pagination: a
-- (sort_value, job_posting_id) cursor with a row-comparison seek, so each page
-- is an index range scan independent of depth, and the per-row total count is
-- dropped (the load-more UI doesn't need it).
--
-- All four sortable columns are effectively non-null in the result set, so the
-- seek needs no NULLS-LAST handling: score is filtered `>= p_min_score` (>= 0);
-- jobs.title and jobs.company_name are NOT NULL; jobs.created_at has a default.
--
-- The tiebreaker is s.job_posting_id (== jp.id), NOT jp.id, so the score seek +
-- ORDER BY live entirely on `scores` and are covered by the composite index
-- below. ORDER BY direction, the seek column/comparator/cast are built with
-- format() from a whitelist (p_sort + p_ascending), so the executed SQL carries
-- a literal ORDER BY + predicate the planner can index; user values travel as
-- USING parameters, so there is no injection surface.
--
-- index-lock-ok: scores is small at beta scale (#101); a brief build lock is
-- acceptable, matching the repo convention for hot-table indexes (#112).

DROP FUNCTION IF EXISTS public.get_target_jobs(
    uuid, integer, text, text, text, text, boolean, integer, integer, uuid
);

CREATE FUNCTION public.get_target_jobs(
    p_target_id uuid,
    p_min_score integer DEFAULT 0,
    p_status text DEFAULT NULL,
    p_company text DEFAULT NULL,
    p_search text DEFAULT NULL,
    p_sort text DEFAULT 'score',
    p_ascending boolean DEFAULT false,
    p_limit integer DEFAULT 20,
    p_after_value text DEFAULT NULL,
    p_after_id uuid DEFAULT NULL,
    p_user_id uuid DEFAULT NULL
) RETURNS TABLE(
    id uuid, external_id text, source_id uuid, title text,
    company_name text, location text, department text,
    absolute_url text, score integer, score_breakdown jsonb,
    scoring_status text, status text, salary_text text,
    greenhouse_updated_at timestamp with time zone,
    first_seen_at timestamp with time zone,
    created_at timestamp with time zone
)
    LANGUAGE plpgsql STABLE
    SET search_path TO ''
    AS $func$
DECLARE
    v_col  text;
    v_cast text;
    v_dir  text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_cmp  text := CASE WHEN p_ascending THEN '>'  ELSE '<'   END;
    v_sql  text;
BEGIN
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
            jp.company_name, jp.location, jp.department,
            jp.absolute_url, s.score, s.score_breakdown,
            s.scoring_status, COALESCE(uj.status, 'new')::text AS status,
            jp.salary_text, jp.greenhouse_updated_at, jp.first_seen_at,
            jp.created_at
        FROM public.scores s
        INNER JOIN public.jobs jp ON jp.id = s.job_posting_id
        LEFT JOIN public.user_jobs uj
            ON uj.job_posting_id = jp.id AND uj.user_id = $1
        WHERE s.target_id = $2
            AND s.excluded = FALSE
            AND s.score >= $3
            AND jp.archived_at IS NULL
            AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
            AND ($5 IS NULL OR jp.company_name = $5)
            AND ($6 IS NULL OR jp.title ILIKE '%%' || $6 || '%%')
            AND ($7 IS NULL OR (%1$s, s.job_posting_id) %2$s ($7::%3$s, $8))
        ORDER BY %1$s %4$s, s.job_posting_id %4$s
        LIMIT $9
    $q$, v_col, v_cmp, v_cast, v_dir);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_id, p_min_score, p_status, p_company,
              p_search, p_after_value, p_after_id, p_limit;
END;
$func$;

-- Composite index covering the keyset seek + ORDER BY for the default (score)
-- sort: target_id + excluded narrow to the target's live scores, then
-- (score DESC, job_posting_id DESC) serves both the row-comparison seek and the
-- ordering as a forward range scan (and an ASC scan reads it backward).
CREATE INDEX IF NOT EXISTS idx_scores_target_excl_score_jpid
    ON public.scores (target_id, excluded, score DESC, job_posting_id DESC);
