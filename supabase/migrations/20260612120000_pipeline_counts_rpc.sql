-- Per-status job counts for a set of targets, in one grouped query.
-- Mirrors the untargeted JWT list view in /jobs: union of jobs scored
-- against any of the caller's targets (excluded = false), with an
-- optional score floor (the user's list_min_score default).
CREATE OR REPLACE FUNCTION "public"."pipeline_counts"("p_target_ids" "uuid"[], "p_min_score" integer) RETURNS TABLE("status" "text", "count" bigint)
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT j.status, COUNT(DISTINCT s.job_posting_id)
  FROM   public.scores s
  JOIN   public.jobs j ON j.id = s.job_posting_id
  WHERE  s.target_id = ANY (p_target_ids)
    AND  s.excluded = false
    AND  (p_min_score IS NULL OR s.score >= p_min_score)
  GROUP BY j.status;
$$;

ALTER FUNCTION "public"."pipeline_counts"("p_target_ids" "uuid"[], "p_min_score" integer) OWNER TO "postgres";

GRANT ALL ON FUNCTION "public"."pipeline_counts"("p_target_ids" "uuid"[], "p_min_score" integer) TO "anon";
GRANT ALL ON FUNCTION "public"."pipeline_counts"("p_target_ids" "uuid"[], "p_min_score" integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."pipeline_counts"("p_target_ids" "uuid"[], "p_min_score" integer) TO "service_role";
