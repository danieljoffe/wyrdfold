-- #93 perf: replace the poller/scoring client-side `.in_()`-chunking paths
-- (merged as the interim URL-truncation safety net in #99) with proper
-- server-side RPCs. The id lists now ride in the POST body as `p_ids jsonb`
-- instead of being encoded into the request URL (~38 chars/UUID), so a
-- thousand-id list can never overflow / silently truncate the PostgREST URL,
-- and each path is one round-trip instead of N chunks.
--
-- Read RPCs mirror get_target_jobs / pipeline_counts: LANGUAGE sql STABLE,
-- fixed search_path, owned by postgres, granted anon/authenticated/service_role.
-- The write RPC mirrors bulk_update_url_health: SECURITY DEFINER, jsonb body
-- param, REVOKE FROM PUBLIC then re-grant.
--
-- Every result is byte-identical to the chunked version it replaces — same
-- columns, types, and (for the dicts/lists the callers fold into) the same
-- set of rows.

-- ---- poller _load_alert_rows: re-read newly-inserted rows post-scoring ------
-- Replaces `jobs.select("*").in_("id", chunk)`. Returns SETOF public.jobs so
-- the row shape is identical to the old `select("*")` (every jobs column).
-- Used to refresh the upsert-time rows (score = column default 0) with the
-- final scores the scoring stages wrote, before alert dispatch.
CREATE OR REPLACE FUNCTION "public"."get_jobs_by_ids"("p_ids" "jsonb")
RETURNS SETOF "public"."jobs"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT j.*
  FROM public.jobs j
  WHERE j.id = ANY (SELECT (jsonb_array_elements_text(p_ids))::uuid);
$$;

ALTER FUNCTION "public"."get_jobs_by_ids"("p_ids" "jsonb") OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."get_jobs_by_ids"("p_ids" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."get_jobs_by_ids"("p_ids" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_jobs_by_ids"("p_ids" "jsonb") TO "service_role";

-- ---- poller _batch_fetch_job_scores: {id: score} lookup --------------------
-- Replaces `jobs.select("id, score").in_("id", chunk)`. Returns exactly the
-- two columns the caller folds into a {job_id: score} dict.
CREATE OR REPLACE FUNCTION "public"."get_job_scores_by_ids"("p_ids" "jsonb")
RETURNS TABLE("id" "uuid", "score" integer)
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT j.id, j.score
  FROM public.jobs j
  WHERE j.id = ANY (SELECT (jsonb_array_elements_text(p_ids))::uuid);
$$;

ALTER FUNCTION "public"."get_job_scores_by_ids"("p_ids" "jsonb") OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."get_job_scores_by_ids"("p_ids" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."get_job_scores_by_ids"("p_ids" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_job_scores_by_ids"("p_ids" "jsonb") TO "service_role";

-- ---- target_scoring get_target_scores: per-target score rows by id ---------
-- Replaces `scores.select("*").eq("target_id", t).in_("job_posting_id", chunk)`.
-- Returns SETOF public.scores so the row shape is identical to the old
-- `select("*")` (every scores column), filtered to one target and the id set.
CREATE OR REPLACE FUNCTION "public"."get_target_scores_by_ids"(
    "p_target_id" "uuid", "p_ids" "jsonb"
)
RETURNS SETOF "public"."scores"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT s.*
  FROM public.scores s
  WHERE s.target_id = p_target_id
    AND s.job_posting_id = ANY (SELECT (jsonb_array_elements_text(p_ids))::uuid);
$$;

ALTER FUNCTION "public"."get_target_scores_by_ids"(
    "p_target_id" "uuid", "p_ids" "jsonb"
) OWNER TO "postgres";
GRANT ALL ON FUNCTION "public"."get_target_scores_by_ids"(
    "p_target_id" "uuid", "p_ids" "jsonb"
) TO "anon";
GRANT ALL ON FUNCTION "public"."get_target_scores_by_ids"(
    "p_target_id" "uuid", "p_ids" "jsonb"
) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_target_scores_by_ids"(
    "p_target_id" "uuid", "p_ids" "jsonb"
) TO "service_role";

-- ---- poller stale-archive: flag delisted jobs globally-dead ----------------
-- Replaces `jobs.update({archived_at, updated_at}).in_("id", chunk)`. Sets
-- one shared `now()` timestamp across every id (matching the single big-UPDATE
-- semantics: the chunked path captured one Python timestamp and reused it
-- across batches; here every row gets the same `now()` evaluated once for the
-- statement) and writes the same two columns the chunked UPDATE wrote.
-- SECURITY DEFINER + REVOKE FROM PUBLIC mirror bulk_update_url_health.
CREATE OR REPLACE FUNCTION "public"."archive_jobs_by_ids"("p_ids" "jsonb") RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
DECLARE
  cnt integer;
  ts  timestamptz := now();
BEGIN
  IF p_ids IS NULL OR jsonb_array_length(p_ids) = 0 THEN
    RETURN 0;
  END IF;

  UPDATE public.jobs jp
  SET    archived_at = ts,
         updated_at  = ts
  WHERE  jp.id = ANY (SELECT (jsonb_array_elements_text(p_ids))::uuid);

  GET DIAGNOSTICS cnt = ROW_COUNT;
  RETURN cnt;
END;
$$;

ALTER FUNCTION "public"."archive_jobs_by_ids"("p_ids" "jsonb") OWNER TO "postgres";

REVOKE ALL ON FUNCTION "public"."archive_jobs_by_ids"("p_ids" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."archive_jobs_by_ids"("p_ids" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."archive_jobs_by_ids"("p_ids" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."archive_jobs_by_ids"("p_ids" "jsonb") TO "service_role";
