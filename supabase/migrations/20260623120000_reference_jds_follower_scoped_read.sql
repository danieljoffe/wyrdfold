-- Follower-scope the reference_jds read policy (audit #29 round 3 M3 / round 1 L6).
--
-- DRAFT — review before applying to prod. This must be (a) merged with the RLS
-- integration suite green in CI (`rls-integration` job, which boots a live
-- Supabase via `supabase start`) and (b) applied to prod Supabase as a
-- deliberate `supabase db push` step. It is forward-only and idempotent.
--
-- The finding: RLS Phase 1 (#79, 20260614120000) gave reference_jds a single
-- permissive SELECT policy `reference_jds_authenticated_read` USING (true) TO
-- authenticated. Combined with #5 P2 (20260622120000) adding the contributor
-- `user_id`, any logged-in user — or anyone replaying the browser-shipped anon
-- key straight at PostgREST — can read EVERY contributed `jd_text` AND the
-- `user_id` that wrote it, deanonymizing the "anonymous" contribution graph
-- (#5 P3 votes are anonymous; the JDs they vote on were not).
--
-- The fix: a user may read a target's reference JDs only if they FOLLOW that
-- target (a row in public.user_targets ties them to it). target_id is uuid on
-- both tables; user_targets.user_id is text and holds the JWT subject, so we
-- cast (select auth.uid()) to text to compare — same shape as the
-- contribution_votes own-row policy (20260622140000).
--
-- (select auth.uid()) — wrapped in a scalar subselect so Postgres evaluates it
-- ONCE per query (initplan) rather than per row (Supabase RLS perf best
-- practice). idx_reference_jds_target_user (target_id, user_id) and
-- user_targets' (user_id, target_id) unique key both back the EXISTS lookup.
--
-- No code change rides with this: the FastAPI backend reads reference_jds via
-- the service-role client (get_supabase -> get_supabase_pool), which BYPASSES
-- RLS, and the frontend never touches reference_jds via supabase-js (it fetches
-- GET /api/targets/:id/reference-jds from the backend). This policy only
-- governs a direct authenticated/anon-key PostgREST read — exactly the
-- deanonymization path the audit flagged. Writes stay service-role-only
-- (no write policy was ever granted to authenticated).

DROP POLICY IF EXISTS "reference_jds_authenticated_read" ON "public"."reference_jds";

CREATE POLICY "reference_jds_follower_read" ON "public"."reference_jds"
    FOR SELECT TO "authenticated"
    USING (
        EXISTS (
            SELECT 1
            FROM "public"."user_targets" "ut"
            WHERE "ut"."target_id" = "reference_jds"."target_id"
              AND "ut"."user_id" = ((SELECT "auth"."uid"() AS "uid"))::"text"
        )
    );
