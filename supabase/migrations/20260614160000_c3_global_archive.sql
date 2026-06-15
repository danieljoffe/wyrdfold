-- #75 C3: split the GLOBAL archive signal out of the per-user jobs.status.
--
-- jobs.status was overloaded: it carried (a) per-user pipeline state and
-- (b) a GLOBAL "this job is dead/gone" signal written by url-health (dead
-- links) and the poller (stale/delisted jobs). Now that the list reads
-- per-user status from user_jobs (#75 C2), globally-archived jobs resurfaced
-- as 'new' for users who never touched them — a regression.
--
-- C3 introduces a dedicated GLOBAL liveness column (jobs.archived_at: NULL =
-- live, non-NULL = globally archived/dead), repoints the global archivers to
-- it, and gates the list + counts on it. The per-user writers stop touching
-- jobs.status (Python side); jobs.status itself is dropped in C4.

-- ---- global liveness column ---------------------------------------------
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS archived_at timestamptz;

-- One-time backfill: jobs currently flagged archived (by url-health/poller
-- under the old overloaded column) become globally-dead.
UPDATE public.jobs
SET archived_at = COALESCE(updated_at, now())
WHERE status = 'archived' AND archived_at IS NULL;

-- ---- get_target_jobs (main paginated list) -------------------------------
-- Same signature as C2; body copied verbatim with one added WHERE clause:
-- AND jp.archived_at IS NULL (exclude globally-dead jobs regardless of the
-- caller's per-user status).
CREATE OR REPLACE FUNCTION "public"."get_target_jobs"(
    "p_target_id" "uuid",
    "p_min_score" integer DEFAULT 0,
    "p_status" "text" DEFAULT NULL::"text",
    "p_company" "text" DEFAULT NULL::"text",
    "p_search" "text" DEFAULT NULL::"text",
    "p_sort" "text" DEFAULT 'score'::"text",
    "p_ascending" boolean DEFAULT false,
    "p_limit" integer DEFAULT 20,
    "p_offset" integer DEFAULT 0,
    "p_user_id" "uuid" DEFAULT NULL::"uuid"
) RETURNS TABLE(
    "id" "uuid", "external_id" "text", "source_id" "uuid", "title" "text",
    "company_name" "text", "location" "text", "department" "text",
    "absolute_url" "text", "score" integer, "score_breakdown" "jsonb",
    "scoring_status" "text", "status" "text", "salary_text" "text",
    "greenhouse_updated_at" timestamp with time zone,
    "first_seen_at" timestamp with time zone,
    "created_at" timestamp with time zone, "total_count" bigint
)
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO ''
    AS $$
BEGIN
  RETURN QUERY
  SELECT
    jp.id,
    jp.external_id,
    jp.source_id,
    jp.title,
    jp.company_name,
    jp.location,
    jp.department,
    jp.absolute_url,
    s.score,
    s.score_breakdown,
    s.scoring_status,
    COALESCE(uj.status, 'new')::"text" AS status,
    jp.salary_text,
    jp.greenhouse_updated_at,
    jp.first_seen_at,
    jp.created_at,
    COUNT(*) OVER () AS total_count
  FROM public.scores s
  INNER JOIN public.jobs jp ON jp.id = s.job_posting_id
  LEFT JOIN public.user_jobs uj
    ON uj.job_posting_id = jp.id AND uj.user_id = p_user_id
  WHERE s.target_id = p_target_id
    AND s.excluded = FALSE
    AND s.score >= p_min_score
    AND jp.archived_at IS NULL
    AND (p_status IS NULL OR COALESCE(uj.status, 'new') = p_status)
    AND (p_company IS NULL OR jp.company_name = p_company)
    AND (p_search IS NULL OR jp.title ILIKE '%' || p_search || '%')
  ORDER BY
    CASE WHEN p_sort = 'score' AND NOT p_ascending THEN s.score END DESC NULLS LAST,
    CASE WHEN p_sort = 'score' AND p_ascending THEN s.score END ASC NULLS LAST,
    CASE WHEN p_sort = 'created_at' AND NOT p_ascending THEN jp.created_at END DESC NULLS LAST,
    CASE WHEN p_sort = 'created_at' AND p_ascending THEN jp.created_at END ASC NULLS LAST,
    CASE WHEN p_sort = 'company_name' AND NOT p_ascending THEN jp.company_name END DESC NULLS LAST,
    CASE WHEN p_sort = 'company_name' AND p_ascending THEN jp.company_name END ASC NULLS LAST,
    CASE WHEN p_sort = 'title' AND NOT p_ascending THEN jp.title END DESC NULLS LAST,
    CASE WHEN p_sort = 'title' AND p_ascending THEN jp.title END ASC NULLS LAST,
    jp.id ASC
  LIMIT p_limit
  OFFSET p_offset;
END;
$$;

-- ---- pipeline_counts (per-status counts) ---------------------------------
-- Same signature as C2; body copied verbatim with one added WHERE clause:
-- AND j.archived_at IS NULL (don't count globally-dead jobs).
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
    AND  (p_min_score IS NULL OR s.score >= p_min_score)
  GROUP BY COALESCE(uj.status, 'new');
$$;
