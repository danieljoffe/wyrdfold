-- #93 perf: kill the truncation-prone `engaged_ids` NOT IN filters.
--
-- Two hot paths (poller existing-rows fetch, url_health candidate select)
-- previously pulled EVERY engaged job id into Python
--   SELECT job_posting_id FROM user_jobs WHERE status <> 'new'
-- then excluded them client-side via PostgREST `.not_.in_("id", ids)`. As
-- user_jobs fills (#75 deploy + user engagement), that id list grows and the
-- generated `id=not.in.(...)` request URL eventually overflows PostgREST's
-- URL length limit and gets TRUNCATED — silently dropping the tail of the
-- exclusion set. A NOT IN over a client-supplied list can't be chunked
-- (chunking ANDs the chunks, which is not the same set), so the only correct
-- fix is to keep the engaged set in Postgres and do the anti-join in SQL.
--
-- These mirror the existing read RPCs (get_target_jobs / pipeline_counts):
-- LANGUAGE sql STABLE, fixed search_path, owned by postgres, granted to
-- anon/authenticated/service_role.

-- ---- poller: live, unengaged jobs for one source ---------------------------
-- Replaces the per-source `existing_query` (jobs for source_id with
-- archived_at IS NULL, minus engaged). Returns exactly the columns the poller
-- reads off `existing_rows` (known_external_ids / (company,title) dedupe).
CREATE OR REPLACE FUNCTION "public"."source_live_unengaged_jobs"("p_source_id" "uuid")
RETURNS TABLE("id" "uuid", "external_id" "text", "title" "text", "company_name" "text")
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT j.id, j.external_id, j.title, j.company_name
  FROM public.jobs j
  WHERE j.source_id = p_source_id
    AND j.archived_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM public.user_jobs uj
                    WHERE uj.job_posting_id = j.id AND uj.status <> 'new');
$$;

ALTER FUNCTION "public"."source_live_unengaged_jobs"("p_source_id" "uuid") OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."source_live_unengaged_jobs"("p_source_id" "uuid") TO "anon";
GRANT ALL ON FUNCTION "public"."source_live_unengaged_jobs"("p_source_id" "uuid") TO "authenticated";
GRANT ALL ON FUNCTION "public"."source_live_unengaged_jobs"("p_source_id" "uuid") TO "service_role";

-- ---- url_health: due, live, unengaged jobs ---------------------------------
-- Replaces `_select_due_jobs`'s null-first + older-than-cutoff two-query merge.
-- The old code ran two PostgREST queries:
--   1. archived_at IS NULL, not engaged, last_url_check_at IS NULL,
--      LIMIT batch_size
--   2. (only if #1 returned < batch_size) archived_at IS NULL, not engaged,
--      last_url_check_at <= cutoff, ORDER BY last_url_check_at ASC,
--      LIMIT (batch_size - len(#1))
-- and concatenated them (#1 rows, then #2 rows).
--
-- This single query is EQUIVALENT: `ORDER BY last_url_check_at ASC NULLS
-- FIRST` puts every NULL row ahead of every non-NULL row, so the first
-- batch_size rows are the same set in the same order as (all NULL rows) then
-- (oldest non-NULL rows) — exactly what the two-query merge produced. The
-- only candidates are NULL or <= cutoff; a non-NULL value > cutoff is
-- excluded by the WHERE, matching query #2's `lte(cutoff)`. (#1 had no cutoff
-- filter, but every NULL row trivially satisfies the OR.)
CREATE OR REPLACE FUNCTION "public"."due_url_health_jobs"(
    "p_cutoff" timestamp with time zone, "p_batch_size" integer
)
RETURNS TABLE("id" "uuid", "absolute_url" "text", "url_check_failure_count" integer)
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT j.id, j.absolute_url, j.url_check_failure_count
  FROM public.jobs j
  WHERE j.archived_at IS NULL
    AND (j.last_url_check_at IS NULL OR j.last_url_check_at <= p_cutoff)
    AND NOT EXISTS (SELECT 1 FROM public.user_jobs uj
                    WHERE uj.job_posting_id = j.id AND uj.status <> 'new')
  ORDER BY j.last_url_check_at ASC NULLS FIRST
  LIMIT p_batch_size;
$$;

ALTER FUNCTION "public"."due_url_health_jobs"(
    "p_cutoff" timestamp with time zone, "p_batch_size" integer
) OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."due_url_health_jobs"(
    "p_cutoff" timestamp with time zone, "p_batch_size" integer
) TO "anon";
GRANT ALL ON FUNCTION "public"."due_url_health_jobs"(
    "p_cutoff" timestamp with time zone, "p_batch_size" integer
) TO "authenticated";
GRANT ALL ON FUNCTION "public"."due_url_health_jobs"(
    "p_cutoff" timestamp with time zone, "p_batch_size" integer
) TO "service_role";
