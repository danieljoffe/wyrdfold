-- #111: tighten SECURITY DEFINER writers.
-- These functions run as `postgres` and bypass RLS. They are only ever
-- invoked via the service-role key — target_scoring (bulk_update_scores),
-- recency (bulk_update_recency_scores), poller url-health + jobs.backfill
-- (bulk_update_url_health, bulk_update_salaries), poller archive
-- (archive_jobs_by_ids), and source discovery (insert_source_if_not_exists).
-- None are reached from the anon or per-user JWT (authenticated) client, so
-- the standing GRANT ... TO anon/authenticated only widens the attack surface:
-- anyone holding the public anon key could call them and mutate the shared
-- catalog. Revoke EXECUTE from anon/authenticated; service_role keeps it.
-- REVOKE is idempotent (no-op if already revoked), so this is re-runnable.
REVOKE ALL ON FUNCTION "public"."bulk_update_scores"("p_updates" "jsonb")
  FROM "anon", "authenticated";
REVOKE ALL ON FUNCTION "public"."bulk_update_salaries"("p_updates" "jsonb")
  FROM "anon", "authenticated";
REVOKE ALL ON FUNCTION "public"."bulk_update_recency_scores"("p_updates" "jsonb")
  FROM "anon", "authenticated";
REVOKE ALL ON FUNCTION "public"."bulk_update_url_health"("p_updates" "jsonb")
  FROM "anon", "authenticated";
REVOKE ALL ON FUNCTION "public"."archive_jobs_by_ids"("p_ids" "jsonb")
  FROM "anon", "authenticated";
REVOKE ALL ON FUNCTION "public"."insert_source_if_not_exists"(
  "p_provider" "text", "p_board_token" "text", "p_company_name" "text"
) FROM "anon", "authenticated";
