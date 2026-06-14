-- RLS Phase 1 (#79) — shared-catalog read policies + service-role-only lockdown.
--
-- Zero effect on today's code: the API reads/writes via the service-role
-- key, which BYPASSES RLS. This only governs what the (not-yet-wired)
-- per-request user-JWT client (authenticated role) can touch — so it
-- safely precedes the Phase 2 read migration.
--
-- Catalog tables (jobs/targets/scores/reference_jds) have RLS enabled
-- with ZERO policies today → deny-all for the authenticated role. Add a
-- permissive SELECT so a logged-in user can READ the shared catalog;
-- writes stay service-role-only (no write policy → RLS denies INSERT/
-- UPDATE/DELETE for authenticated even though the GRANT exists). Per-user
-- *visibility* (which jobs you see) remains the scores⋈user_targets join
-- in Python — RLS gives table access, not the cross-table rule.

CREATE POLICY "jobs_authenticated_read" ON "public"."jobs"
    FOR SELECT TO "authenticated" USING (true);

CREATE POLICY "targets_authenticated_read" ON "public"."targets"
    FOR SELECT TO "authenticated" USING (true);

CREATE POLICY "scores_authenticated_read" ON "public"."scores"
    FOR SELECT TO "authenticated" USING (true);

CREATE POLICY "reference_jds_authenticated_read" ON "public"."reference_jds"
    FOR SELECT TO "authenticated" USING (true);

-- Service-role-only tables: operator/background/auth-hook surface that a
-- user JWT must never reach. RLS-on/0-policies already denies the
-- authenticated role; revoke the table GRANTs too (defense in depth — a
-- missing future policy shouldn't be the only thing standing between a
-- user JWT and these rows). service_role keeps full access (it bypasses
-- RLS and retains its own grant); supabase_auth_admin keeps its grant on
-- wyrdfold_beta_invites for the `hook_restrict_wyrdfold_beta` auth hook.
REVOKE ALL ON TABLE "public"."sources" FROM "anon", "authenticated";
REVOKE ALL ON TABLE "public"."source_discoveries" FROM "anon", "authenticated";
REVOKE ALL ON TABLE "public"."target_derive_jd_cache" FROM "anon", "authenticated";
REVOKE ALL ON TABLE "public"."wyrdfold_beta_invites" FROM "anon", "authenticated";
