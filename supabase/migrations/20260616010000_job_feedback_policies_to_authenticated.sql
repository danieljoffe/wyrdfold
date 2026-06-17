-- #113: job_feedback's RLS policies were created without a TO clause, so they
-- apply to PUBLIC (incl. the anon role). They're deny-closed by the JWT
-- predicate (auth.jwt()->>'sub' is NULL for anon, so the check fails), but
-- that's inconsistent with the rest of the schema (e.g. user_targets uses
-- "TO authenticated"). Recreate them scoped TO authenticated.
--
-- Behavior is unchanged: real users carry the `authenticated` role and the
-- same predicate; anon was already denied and is still denied (RLS on + no
-- applicable policy = deny); the service-role client bypasses RLS regardless.
DROP POLICY IF EXISTS "job_feedback_self_select" ON "public"."job_feedback";
CREATE POLICY "job_feedback_self_select" ON "public"."job_feedback"
  FOR SELECT TO "authenticated"
  USING (((SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));

DROP POLICY IF EXISTS "job_feedback_self_insert" ON "public"."job_feedback";
CREATE POLICY "job_feedback_self_insert" ON "public"."job_feedback"
  FOR INSERT TO "authenticated"
  WITH CHECK (((SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));

DROP POLICY IF EXISTS "job_feedback_self_update" ON "public"."job_feedback";
CREATE POLICY "job_feedback_self_update" ON "public"."job_feedback"
  FOR UPDATE TO "authenticated"
  USING (((SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));

DROP POLICY IF EXISTS "job_feedback_self_delete" ON "public"."job_feedback";
CREATE POLICY "job_feedback_self_delete" ON "public"."job_feedback"
  FOR DELETE TO "authenticated"
  USING (((SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));
