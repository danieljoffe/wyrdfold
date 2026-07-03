-- #191 slice 1b — bounded-delta + staging for the reference-JD merge path.
--
-- 20260703090000 RPC-gated the LEARNER's shared-profile writes; the
-- reference-JD merge path still wrote `targets.scoring_profile` raw and
-- unbounded — one malicious (or just skewed) JD flowed into the shared
-- profile immediately, with suppression/votes only able to correct it
-- after the fact. This migration adds the two DB pieces of the fix:
--
-- 1. `target_learning_log` grows a `kind` discriminator ('patch' = the
--    existing learner rows, 'merge' = a quarantined reference-JD merge
--    awaiting review) and a `merge_payload` jsonb carrying what the apply
--    needs (the quarantined ref_jd_id + the derived search_keywords /
--    example-title pools that only update when the merge actually lands).
--    Quarantine itself reuses `reference_jds.suppressed`: a capped
--    contribution is inserted suppressed-at-birth, so every existing merge
--    already excludes it; apply = unsuppress + fresh re-merge, reject =
--    stays suppressed (the vote quorum can still rescue it later).
--
-- 2. `apply_target_profile_merge` — the merge-shaped sibling of
--    `apply_target_profile_patch`: same FOR UPDATE serialization, in-DB
--    follower re-check, and optimistic version guard, plus the merge's
--    extra columns (search_keywords jsonb, example title pools text[]),
--    each written only when a non-NULL value is passed.
--
-- Grants follow the locked-down convention (20260621150000 /
-- 20260630120000 / 20260703090000): owner postgres, PUBLIC/anon/
-- authenticated revoked, EXECUTE to service_role only.

ALTER TABLE public.target_learning_log
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'patch'
        CONSTRAINT target_learning_log_kind_check CHECK (kind IN ('patch', 'merge')),
    ADD COLUMN IF NOT EXISTS merge_payload jsonb;

CREATE OR REPLACE FUNCTION public.apply_target_profile_merge(
    p_user_id uuid,
    p_target_id uuid,
    p_next_profile jsonb,
    p_expected_version integer,
    p_search_keywords jsonb DEFAULT NULL,
    p_example_promising text[] DEFAULT NULL,
    p_example_unpromising text[] DEFAULT NULL
) RETURNS TABLE(outcome text, new_version integer)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path TO 'public', 'pg_catalog'
AS $$
DECLARE
    v_version integer;
BEGIN
    -- Serialize concurrent shared-profile writes on this target.
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

    -- In-DB follower re-check (defense-in-depth under the router's check).
    IF NOT EXISTS (
        SELECT 1 FROM public.user_targets ut
        WHERE ut.user_id = p_user_id AND ut.target_id = p_target_id
    ) THEN
        outcome := 'not_a_follower';
        new_version := v_version;
        RETURN NEXT;
        RETURN;
    END IF;

    -- Optimistic concurrency: a merge computed against a stale profile
    -- version must be recomputed by the caller, never clobber a newer one.
    IF v_version IS DISTINCT FROM p_expected_version THEN
        outcome := 'version_conflict';
        new_version := v_version;
        RETURN NEXT;
        RETURN;
    END IF;

    UPDATE public.targets
       SET scoring_profile = p_next_profile,
           profile_version = v_version + 1,
           search_keywords = COALESCE(p_search_keywords, search_keywords),
           example_promising_titles =
               COALESCE(p_example_promising, example_promising_titles),
           example_unpromising_titles =
               COALESCE(p_example_unpromising, example_unpromising_titles),
           updated_at = now()
     WHERE id = p_target_id;

    outcome := 'applied';
    new_version := v_version + 1;
    RETURN NEXT;
END;
$$;

ALTER FUNCTION public.apply_target_profile_merge(
    uuid, uuid, jsonb, integer, jsonb, text[], text[]
) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.apply_target_profile_merge(
    uuid, uuid, jsonb, integer, jsonb, text[], text[]
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.apply_target_profile_merge(
    uuid, uuid, jsonb, integer, jsonb, text[], text[]
) TO service_role;
