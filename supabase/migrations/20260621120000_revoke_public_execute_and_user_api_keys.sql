-- #2 audit — live-DB findings (2026-06-21). The migration-file analysis behind
-- #111 / #14 / #23 missed that **Postgres grants EXECUTE to PUBLIC on every new
-- function by default**. Those revokes named only anon/authenticated, so any
-- function whose anon access comes via PUBLIC stayed callable. Verified against
-- the live project: `proacl` shows `=X/postgres` (a PUBLIC EXECUTE grant) on the
-- functions below. REVOKE is idempotent, so this is re-runnable.

-- A. SECURITY DEFINER writers still anon-callable via PUBLIC.
--    bulk_update_recency_scores + insert_source_if_not_exists hold a PUBLIC
--    EXECUTE (not an explicit anon grant), so the #111 `REVOKE FROM anon,
--    authenticated` was a no-op for them. They run as postgres (bypass RLS) and
--    mutate the shared catalog — so an unauthenticated anon-key caller can
--    currently invoke them (corrupt recency scores / insert arbitrary sources).
--    Revoke PUBLIC (+ anon/authenticated belt-and-suspenders); service_role keeps it.
REVOKE ALL ON FUNCTION "public"."bulk_update_recency_scores"("p_updates" "jsonb")
  FROM PUBLIC, "anon", "authenticated";
REVOKE ALL ON FUNCTION "public"."insert_source_if_not_exists"(
  "p_provider" "text", "p_board_token" "text", "p_company_name" "text"
) FROM PUBLIC, "anon", "authenticated";

-- B. Per-user spend RPCs. #14 added the in-body auth.uid() guard and revoked the
--    explicit anon grant, but these ALSO hold a PUBLIC EXECUTE that defeated the
--    revoke (anon still executes via PUBLIC). Revoke PUBLIC + anon so only
--    `authenticated` (guarded to its own uid + RLS-scoped, INVOKER) and
--    service_role remain.
REVOKE ALL ON FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone)
  FROM PUBLIC, "anon";
REVOKE ALL ON FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone)
  FROM PUBLIC, "anon";
REVOKE ALL ON FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone)
  FROM PUBLIC, "anon";
-- total_spend_all_since is service-role-only (global circuit breaker); the #23
--   revoke also missed its PUBLIC grant.
REVOKE ALL ON FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone)
  FROM PUBLIC, "anon", "authenticated";

-- C. Beta-gate auth hook (SECURITY DEFINER) is anon/authenticated-callable via
--    explicit grants. It is invoked by GoTrue as supabase_auth_admin, which
--    retains EXECUTE (verified live) — so revoking anon/authenticated does NOT
--    break signups. Mirrors #111.
REVOKE ALL ON FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb")
  FROM "anon", "authenticated";

-- D. user_api_keys stores AES-256-GCM-encrypted BYOK provider keys. It is RLS-on
--    with ZERO policy (denied today), but Supabase's default privileges left a
--    full GRANT to anon/authenticated on the table — the same inert-but-wrong
--    surface #14 removed for notifications_sent, on a far more sensitive table.
--    Table grants are explicit (tables don't get the function PUBLIC default),
--    so naming the roles suffices. service_role keeps ALL.
REVOKE ALL ON TABLE "public"."user_api_keys" FROM "anon", "authenticated";
