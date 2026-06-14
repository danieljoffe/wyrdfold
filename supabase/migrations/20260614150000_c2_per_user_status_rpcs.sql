-- #75 C2: cut the jobs list + pipeline counts over to per-user status.
--
-- Both RPCs previously read the global jobs.status. They now LEFT JOIN
-- user_jobs for the calling user and resolve status as
-- COALESCE(uj.status, 'new') — a posting the user hasn't touched has no
-- user_jobs row and reads as 'new' (the #75 "absent = new" rule). When
-- p_user_id is NULL (api-key/cron path, which has no per-user pipeline)
-- the join never matches, so everything resolves to 'new'.
--
-- The PK on user_jobs(user_id, job_posting_id) already serves the
-- equality join on both columns, so no extra index is needed.
--
-- Adding a parameter changes each function's signature, so we DROP the old
-- signature and CREATE the new one (and re-GRANT, since DROP removes grants).

-- ---- get_target_jobs (main paginated list) -------------------------------
DROP FUNCTION IF EXISTS "public"."get_target_jobs"(
    "uuid", integer, "text", "text", "text", "text", boolean, integer, integer
);

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

ALTER FUNCTION "public"."get_target_jobs"(
    "uuid", integer, "text", "text", "text", "text", boolean, integer, integer, "uuid"
) OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."get_target_jobs"(
    "uuid", integer, "text", "text", "text", "text", boolean, integer, integer, "uuid"
) TO "anon";
GRANT ALL ON FUNCTION "public"."get_target_jobs"(
    "uuid", integer, "text", "text", "text", "text", boolean, integer, integer, "uuid"
) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_target_jobs"(
    "uuid", integer, "text", "text", "text", "text", boolean, integer, integer, "uuid"
) TO "service_role";

-- ---- pipeline_counts (per-status counts) ---------------------------------
DROP FUNCTION IF EXISTS "public"."pipeline_counts"("uuid"[], integer);

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
    AND  (p_min_score IS NULL OR s.score >= p_min_score)
  GROUP BY COALESCE(uj.status, 'new');
$$;

ALTER FUNCTION "public"."pipeline_counts"("uuid"[], integer, "uuid") OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."pipeline_counts"("uuid"[], integer, "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."pipeline_counts"("uuid"[], integer, "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."pipeline_counts"("uuid"[], integer, "uuid") TO "service_role";
