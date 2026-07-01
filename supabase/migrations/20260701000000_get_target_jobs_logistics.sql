-- #86: return `logistics_filters` from the get_target_jobs keyset RPC.
--
-- The /jobs two-query paths already SELECT + overlay `scores.logistics_filters`
-- (#138), but the keyset RPC fast path — taken for the non-score sorts
-- (created_at / title / company_name) with no floor / location / multi-word
-- search — did not, so the logistics chips were absent on those sorts. This adds
-- the one column to RETURNS TABLE + the SELECT; EVERYTHING ELSE is reproduced
-- VERBATIM from 20260616030000 (the injection-safe format() keyset seek, the
-- whitelist CASE, `SET search_path TO ''`). SECURITY INVOKER (unchanged), so RLS
-- still governs what each caller reads; the drop+recreate restores the same
-- default grants (PUBLIC/anon/authenticated/service_role EXECUTE) it had.
-- Additive, non-destructive; the covering index from 20260616030000 is untouched.

DROP FUNCTION IF EXISTS public.get_target_jobs(
    uuid, integer, text, text, text, text, boolean, integer, text, uuid, uuid
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
    scoring_status text, logistics_filters jsonb, status text, salary_text text,
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
            s.scoring_status, s.logistics_filters,
            COALESCE(uj.status, 'new')::text AS status,
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
