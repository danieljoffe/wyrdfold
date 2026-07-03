-- #191 (shared-target contribution hardening) — RPC-gate the learner's
-- shared-profile writes.
--
-- `llm_learner` (auto-apply and staged-apply) mutated the SHARED
-- `targets.scoring_profile` with a raw `.update()` on the service-role
-- client. The only thing standing between any authenticated caller and a
-- profile every follower shares was a Python-side follower check — a Python
-- authz bug (or a new caller skipping the check) silently poisons scoring
-- for every user of that target. The read-increment of `profile_version`
-- was also racy: two concurrent applies could both read v1 and write v2,
-- losing one patch entirely.
--
-- Fix: move the write into one SECURITY DEFINER function that
--   1. takes a `FOR UPDATE` lock on the target row (concurrent applies
--      serialize — same discipline as 20260630120000's suppression RPC);
--   2. re-checks *inside the DB* that the acting user actually follows the
--      target (`user_targets` row exists);
--   3. enforces optimistic concurrency: the caller passes the
--      `profile_version` its patch was computed against, and a moved
--      version is refused as `version_conflict` (the caller re-stages) —
--      a patch computed against a stale profile can never clobber a newer
--      one.
--
-- Grants follow the locked-down convention (20260621150000 /
-- 20260630120000): owner postgres, PUBLIC/anon/authenticated revoked,
-- EXECUTE to service_role only — the backend calls it exclusively on the
-- service-role client, passing the JWT-derived user id.

CREATE OR REPLACE FUNCTION public.apply_target_profile_patch(
    p_user_id uuid,
    p_target_id uuid,
    p_next_profile jsonb,
    p_expected_version integer
) RETURNS TABLE(outcome text, new_version integer)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path TO 'public', 'pg_catalog'
AS $$
DECLARE
    v_version integer;
BEGIN
    -- Serialize concurrent applies on this target (see header).
    SELECT t.profile_version INTO v_version
    FROM public.targets t
    WHERE t.id = p_target_id
    FOR UPDATE;

    IF NOT FOUND THEN
        outcome := 'target_not_found';
        new_version := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    -- In-DB follower re-check: only a user actually linked to the target
    -- may move its shared profile. Defense-in-depth under the routers'
    -- Python-side ownership checks.
    IF NOT EXISTS (
        SELECT 1 FROM public.user_targets ut
        WHERE ut.user_id = p_user_id AND ut.target_id = p_target_id
    ) THEN
        outcome := 'not_a_follower';
        new_version := v_version;
        RETURN NEXT;
        RETURN;
    END IF;

    -- Optimistic concurrency: refuse a patch computed against a profile
    -- that has since moved (concurrent learn, reference-JD merge, …).
    IF v_version IS DISTINCT FROM p_expected_version THEN
        outcome := 'version_conflict';
        new_version := v_version;
        RETURN NEXT;
        RETURN;
    END IF;

    UPDATE public.targets
       SET scoring_profile = p_next_profile,
           profile_version = v_version + 1,
           updated_at = now()
     WHERE id = p_target_id;

    outcome := 'applied';
    new_version := v_version + 1;
    RETURN NEXT;
END;
$$;

ALTER FUNCTION public.apply_target_profile_patch(uuid, uuid, jsonb, integer)
    OWNER TO postgres;
REVOKE ALL ON FUNCTION public.apply_target_profile_patch(uuid, uuid, jsonb, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_target_profile_patch(uuid, uuid, jsonb, integer)
    TO service_role;
