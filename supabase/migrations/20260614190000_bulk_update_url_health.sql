-- Bulk-update url-health results in a single statement (perf #93).
-- Mirrors bulk_update_scores / bulk_update_salaries: one jsonb payload,
-- one set-based UPDATE ... FROM, instead of one round-trip per job. The
-- url_health tick HEADs up to URL_HEALTH_BATCH_SIZE (~50) jobs and used
-- to write each result with its own UPDATE; this collapses that loop into
-- a single RPC call. Each element is {id, last_url_check_at,
-- url_check_status, url_check_failure_count} matching jobs' column types.
CREATE OR REPLACE FUNCTION "public"."bulk_update_url_health"("p_updates" "jsonb") RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
DECLARE
  cnt integer;
BEGIN
  IF p_updates IS NULL OR jsonb_array_length(p_updates) = 0 THEN
    RETURN 0;
  END IF;

  WITH u AS (
    SELECT *
    FROM   jsonb_to_recordset(p_updates) AS x(
             id                       uuid,
             last_url_check_at        timestamptz,
             url_check_status         integer,
             url_check_failure_count  integer
           )
  )
  UPDATE public.jobs jp
  SET    last_url_check_at       = u.last_url_check_at,
         url_check_status        = u.url_check_status,
         url_check_failure_count = u.url_check_failure_count
  FROM   u
  WHERE  jp.id = u.id;

  GET DIAGNOSTICS cnt = ROW_COUNT;
  RETURN cnt;
END;
$$;

ALTER FUNCTION "public"."bulk_update_url_health"("p_updates" "jsonb") OWNER TO "postgres";

REVOKE ALL ON FUNCTION "public"."bulk_update_url_health"("p_updates" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."bulk_update_url_health"("p_updates" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."bulk_update_url_health"("p_updates" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."bulk_update_url_health"("p_updates" "jsonb") TO "service_role";
