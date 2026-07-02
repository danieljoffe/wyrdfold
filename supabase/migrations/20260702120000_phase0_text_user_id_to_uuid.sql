-- Phase 0 (deployment-modes): convert the four remaining text user_id columns
-- to uuid and give them the account-cascade FK.
--
-- 20260701170000 added `user_id -> auth.users(id) ON DELETE CASCADE` to the 13
-- uuid per-user tables. The four tables here (user_targets, job_feedback,
-- contribution_votes, target_learning_log) were carved out because their
-- user_id is `text` — a leftover from the pre-Supabase-auth era. Every stored
-- value is a valid uuid that exists in auth.users (verified in prod
-- 2026-07-01); the `USING user_id::uuid` cast below re-verifies that at deploy
-- time (a non-uuid value aborts the migration, which is the correct failure
-- mode), and the ADD CONSTRAINT validates existence in auth.users.
--
-- Order of operations matters:
--   1. DROP the RLS policies — they compare user_id as text
--      (`auth.jwt()->>'sub' = user_id` / `auth.uid()::text = user_id`) and a
--      column referenced by a policy cannot change type.
--   2. ALTER COLUMN ... TYPE uuid. Indexes and unique constraints on user_id
--      are rebuilt automatically.
--   3. Recreate each policy with the native-uuid comparison
--      `(select auth.uid()) = user_id`, preserving each policy's exact
--      cmd/roles/USING/WITH CHECK semantics (including
--      target_learning_log_self_select's original no-TO-clause / PUBLIC
--      scoping — it stays deny-closed for anon because auth.uid() is NULL and
--      20260620120000 revoked anon table grants). This also retires the last
--      `auth.jwt()->>'sub'` and `::text` casts from these policies.
--      reference_jds_follower_read (20260623120000) is included because its
--      EXISTS clause compares user_targets.user_id — the comparison changes
--      to native uuid but the policy's follower-scoping semantics (and the
--      reference_jds table itself, which pends a separate design call) are
--      untouched.
--   4. Recreate the three SECURITY DEFINER score-write RPCs whose ownership
--      gates compare `user_targets.user_id = (SELECT auth.uid())::text` —
--      after the type change that expression would raise
--      `operator does not exist: uuid = text` at runtime. CREATE OR REPLACE
--      preserves their existing owner/grants (20260621140000/20260621150000).
--   5. Make the sync_target_active() trigger function SECURITY DEFINER.
--      Without this the FK cascade is a trap: deleting an auth.users row via
--      the GoTrue admin API runs as `supabase_auth_admin`, whose cascade
--      DELETE on user_targets fires trg_sync_target_active — and the
--      function's `UPDATE public.targets` fails with `permission denied for
--      table targets` (supabase_auth_admin has no grant on public.targets),
--      aborting the whole user deletion (reproduced locally: GoTrue 500
--      "Database error deleting user"). The function maintains derived state
--      (targets.is_active from user_targets rows); the DML that fires it is
--      already gated by grants + RLS on user_targets, so it must succeed
--      regardless of WHICH legitimate role caused the row change — the
--      textbook SECURITY DEFINER trigger. Locked down per 20260629120000
--      (owner postgres; EXECUTE revoked from PUBLIC/anon/authenticated —
--      trigger firing doesn't require caller EXECUTE).
--   6. ADD the account-cascade FK, matching 20260701170000's pattern.
--
-- App impact: none. supabase-py/PostgREST serialize uuid columns as their
-- canonical text form, so the API sends and receives the same strings as
-- before (all stored values were already canonical lowercase uuids).

-- 1. Drop the text-comparison policies -----------------------------------------

DROP POLICY "Users access their own user_targets"       ON "public"."user_targets";
DROP POLICY "Users access their own contribution_votes" ON "public"."contribution_votes";
DROP POLICY "job_feedback_self_select"                  ON "public"."job_feedback";
DROP POLICY "job_feedback_self_insert"                  ON "public"."job_feedback";
DROP POLICY "job_feedback_self_update"                  ON "public"."job_feedback";
DROP POLICY "job_feedback_self_delete"                  ON "public"."job_feedback";
DROP POLICY "target_learning_log_self_select"           ON "public"."target_learning_log";
DROP POLICY "reference_jds_follower_read"                ON "public"."reference_jds";

-- 2. Convert text -> uuid (aborts if any stored value is not a valid uuid) -----

ALTER TABLE "public"."user_targets"        ALTER COLUMN "user_id" TYPE uuid USING "user_id"::uuid;
ALTER TABLE "public"."job_feedback"        ALTER COLUMN "user_id" TYPE uuid USING "user_id"::uuid;
ALTER TABLE "public"."contribution_votes"  ALTER COLUMN "user_id" TYPE uuid USING "user_id"::uuid;
ALTER TABLE "public"."target_learning_log" ALTER COLUMN "user_id" TYPE uuid USING "user_id"::uuid;

-- 3. Recreate the policies with the native-uuid comparison ---------------------

CREATE POLICY "Users access their own user_targets"
    ON "public"."user_targets" TO "authenticated"
    USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"))
    WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));

CREATE POLICY "Users access their own contribution_votes"
    ON "public"."contribution_votes" TO "authenticated"
    USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"))
    WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));

CREATE POLICY "job_feedback_self_select" ON "public"."job_feedback"
  FOR SELECT TO "authenticated"
  USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"));

CREATE POLICY "job_feedback_self_insert" ON "public"."job_feedback"
  FOR INSERT TO "authenticated"
  WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));

CREATE POLICY "job_feedback_self_update" ON "public"."job_feedback"
  FOR UPDATE TO "authenticated"
  USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"));

CREATE POLICY "job_feedback_self_delete" ON "public"."job_feedback"
  FOR DELETE TO "authenticated"
  USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"));

-- Deliberately no TO clause: the original policy (20260612015641) applies to
-- PUBLIC and this migration preserves semantics; anon remains deny-closed
-- (NULL auth.uid() + no anon table grants since 20260620120000).
CREATE POLICY "target_learning_log_self_select" ON "public"."target_learning_log"
  FOR SELECT
  USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"));

-- Verbatim from 20260623120000 minus the ::text cast (user_targets.user_id is
-- now uuid). Follower-scoping semantics unchanged.
CREATE POLICY "reference_jds_follower_read" ON "public"."reference_jds"
    FOR SELECT TO "authenticated"
    USING (
        EXISTS (
            SELECT 1
            FROM "public"."user_targets" "ut"
            WHERE "ut"."target_id" = "reference_jds"."target_id"
              AND "ut"."user_id" = (SELECT "auth"."uid"() AS "uid")
        )
    );

-- 4. Recreate the DEFINER RPCs whose ownership gate compared text --------------
-- Bodies verbatim from 20260621140000 / 20260621150000 except the
-- `(SELECT auth.uid())::text` comparison, now native uuid. Owner + grants
-- (authenticated, service_role; PUBLIC revoked) survive CREATE OR REPLACE.

CREATE OR REPLACE FUNCTION public.user_apply_score_blend(
    p_job_posting_id uuid,
    p_target_id uuid,
    p_score integer,
    p_analysis_id uuid
) RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path TO 'public', 'pg_catalog'
AS $$
BEGIN
    -- Ownership gate mirroring the user_targets RLS policy
    -- ((select auth.uid()) = user_id). A JWT caller may only blend a
    -- score for a target they follow; service-role (auth.uid() NULL) bypasses,
    -- matching the poller/operator path.
    IF auth.uid() IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.user_targets
        WHERE user_id = (SELECT auth.uid())
          AND target_id = p_target_id
    ) THEN
        RAISE EXCEPTION 'not authorized to score target %', p_target_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    UPDATE public.scores
       SET score = p_score,
           scoring_status = 'complete'
     WHERE job_posting_id = p_job_posting_id
       AND target_id = p_target_id;

    UPDATE public.jobs
       SET llm_analysis_id = p_analysis_id
     WHERE id = p_job_posting_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.user_upsert_score(p_row jsonb)
    RETURNS SETOF public.scores
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path TO 'public', 'pg_catalog'
AS $$
DECLARE
    v_target uuid := (p_row->>'target_id')::uuid;
BEGIN
    IF auth.uid() IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.user_targets
        WHERE user_id = (SELECT auth.uid())
          AND target_id = v_target
    ) THEN
        RAISE EXCEPTION 'not authorized to score target %', v_target
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN QUERY
    INSERT INTO public.scores (
        job_posting_id, target_id, score, score_breakdown, matched_keywords,
        excluded, scoring_status, scored_profile_version, recency_score, updated_at
    ) VALUES (
        (p_row->>'job_posting_id')::uuid,
        v_target,
        (p_row->>'score')::int,
        p_row->'score_breakdown',
        ARRAY(SELECT jsonb_array_elements_text(p_row->'matched_keywords')),
        (p_row->>'excluded')::boolean,
        p_row->>'scoring_status',
        (p_row->>'scored_profile_version')::int,
        (p_row->>'recency_score')::int,
        (p_row->>'updated_at')::timestamptz
    )
    ON CONFLICT (job_posting_id, target_id) DO UPDATE SET
        score = EXCLUDED.score,
        score_breakdown = EXCLUDED.score_breakdown,
        matched_keywords = EXCLUDED.matched_keywords,
        excluded = EXCLUDED.excluded,
        scoring_status = EXCLUDED.scoring_status,
        scored_profile_version = EXCLUDED.scored_profile_version,
        recency_score = EXCLUDED.recency_score,
        updated_at = EXCLUDED.updated_at
    RETURNING *;
END;
$$;

CREATE OR REPLACE FUNCTION public.user_set_scores_included(
    p_job_posting_id uuid,
    p_target_ids uuid[]
) RETURNS void
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path TO 'public', 'pg_catalog'
AS $$
BEGIN
    IF auth.uid() IS NOT NULL AND EXISTS (
        SELECT 1 FROM unnest(p_target_ids) AS t(tid)
        WHERE NOT EXISTS (
            SELECT 1 FROM public.user_targets
            WHERE user_id = (SELECT auth.uid())
              AND target_id = t.tid
        )
    ) THEN
        RAISE EXCEPTION 'not authorized for one or more targets'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    UPDATE public.scores
       SET excluded = false
     WHERE job_posting_id = p_job_posting_id
       AND target_id = ANY(p_target_ids);
END;
$$;

-- 5. sync_target_active must not depend on the cascading role's grants --------
-- Body verbatim from 20260612015641 (only SECURITY DEFINER is new).

CREATE OR REPLACE FUNCTION "public"."sync_target_active"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SECURITY DEFINER
    SET "search_path" TO 'public', 'extensions'
    AS $$
DECLARE
  _target_id UUID;
BEGIN
  IF TG_OP = 'DELETE' THEN
    _target_id := OLD.target_id;
  ELSE
    _target_id := NEW.target_id;
  END IF;

  UPDATE public.targets SET is_active = EXISTS(
    SELECT 1 FROM public.user_targets
    WHERE target_id = _target_id AND is_active = TRUE
  ) WHERE id = _target_id;

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

-- Now that it's DEFINER, apply the standing trigger-fn lockdown (20260629120000
-- pattern): the remote_schema GRANT ALL to anon/authenticated predates it.
REVOKE ALL ON FUNCTION public.sync_target_active()
  FROM PUBLIC, anon, authenticated;

-- 6. Account-cascade FK (validates existing rows; matches 20260701170000) ------

ALTER TABLE public.user_targets        ADD CONSTRAINT user_targets_user_id_fkey        FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.job_feedback        ADD CONSTRAINT job_feedback_user_id_fkey        FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.contribution_votes  ADD CONSTRAINT contribution_votes_user_id_fkey  FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.target_learning_log ADD CONSTRAINT target_learning_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
