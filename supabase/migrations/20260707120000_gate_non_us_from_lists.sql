-- #60: gate CONFIRMED non-US jobs out of the user-facing list + count RPCs.
--
-- The display layer excludes non-US jobs ONLY via jobs.archived_at (the #246
-- tagger stamps it high-confidence non-US). That bet leaks: the archive fires
-- only at fresh-tag time, so jobs tagged before it shipped — or re-polled via
-- the content-hash cache — stay is_us=false + unarchived and surface as matches
-- (a Bogotá/Santo Domingo job scored 100 on a frontend target). This adds a
-- second, defense-in-depth gate so a missed archive can't leak:
--
--   AND <j>.is_us IS NOT FALSE   -- keep US (true) + not-yet-tagged (null),
--                                -- drop only CONFIRMED non-US (false)
--
-- Both functions are reproduced VERBATIM from their latest defs
-- (get_target_jobs: 20260701000000_get_target_jobs_logistics;
--  pipeline_counts: 20260614160000_c3_global_archive) with the single clause
-- added next to the existing `archived_at IS NULL` gate. The Python fallbacks
-- (_fetch_jobs_chunked / _pipeline_counts_python) get the same gate in
-- app/routers/jobs.py (_gate_live_us). Additive, non-destructive; SECURITY
-- INVOKER unchanged (RLS still governs); DROP+CREATE restores the same default
-- EXECUTE grants; CREATE OR REPLACE preserves pipeline_counts' grants.

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
            AND jp.is_us IS NOT FALSE
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

-- ---- pipeline_counts (per-status counts) ---------------------------------
-- Verbatim from 20260614160000_c3_global_archive with one added WHERE clause:
-- AND j.is_us IS NOT FALSE (don't count confirmed non-US jobs).
CREATE OR REPLACE FUNCTION "public"."pipeline_counts"(
    "p_target_ids" "uuid"[], "p_min_score" integer, "p_user_id" "uuid" DEFAULT NULL::"uuid"
) RETURNS TABLE("status" "text", "count" bigint)
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(uj.status, 'new') AS status, COUNT(DISTINCT s.job_posting_id)
  FROM   public.scores s
  JOIN   public.jobs j ON j.id = s.job_posting_id
  LEFT JOIN public.user_jobs uj
    ON uj.job_posting_id = j.id AND uj.user_id = p_user_id
  WHERE  s.target_id = ANY (p_target_ids)
    AND  s.excluded = false
    AND  j.archived_at IS NULL
    AND  j.is_us IS NOT FALSE
    AND  (p_min_score IS NULL OR s.score >= p_min_score)
  GROUP BY COALESCE(uj.status, 'new');
$$;
