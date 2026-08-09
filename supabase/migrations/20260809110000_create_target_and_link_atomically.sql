-- #667 follow-up: creating a target and following it become ONE transaction.
--
-- THE PROBLEM THIS REMOVES. The create-or-link paths did three separate
-- PostgREST round-trips:
--     1. INSERT the target        (app_active false, no membership yet)
--     2. UPDATE activation_status
--     3. INSERT the user_targets membership
-- Between 1 and 3 the row is `app_active = false` with zero memberships —
-- character-for-character the definition of an orphaned target. So "orphan" was
-- not a detectable state, it was a state the system passes THROUGH on the happy
-- path, and any cleanup predicate had to guess (by age) whether a row was being
-- born or abandoned. That guess is what this removes: with creation atomic,
-- `NOT app_active AND no memberships` becomes an unambiguously invalid state.
--
-- It also closes a real, if rare, bug on the create side: if step 3 failed after
-- step 1 succeeded, the user got an error AND a permanent orphan. Now either both
-- rows exist or neither does.
--
-- (The catalog seed had the same shape and a far wider window — an LLM call
-- between insert and sponsorship. That one is fixed in Python by sponsoring at
-- birth: `crud.create(..., app_active=True)`.)
--
-- SEMANTICS ARE PRESERVED EXACTLY, including the fiddly bits:
--   * find-or-create on `normalized_label` stays idempotent and race-safe. The
--     old code used `ignore_duplicates` (DO NOTHING) and explicitly never
--     overwrote the canonical row, then separately UPDATEd `activation_status`.
--     `DO UPDATE SET activation_status` reproduces that net effect in one
--     statement — and touches ONLY that column, so a co-followed catalog row
--     keeps its label/profile/keywords exactly as before.
--   * the membership is always `is_active = false` (following never trips the
--     active-target cap; activation is a separate step), so
--     `trg_enforce_active_target_ceiling` still fires but cannot reject.
--   * both rows come back, because the callers build their response from each.
--
-- SECURITY INVOKER (the default) is deliberate: every caller already holds the
-- service-role client, so DEFINER would add privilege without adding capability.

CREATE OR REPLACE FUNCTION public.create_target_and_link(
    p_user_id uuid,
    p_label text,
    p_normalized_label text,
    p_activation_status text DEFAULT NULL,
    p_description text DEFAULT NULL,
    p_scoring_profile jsonb DEFAULT '{}'::jsonb,
    p_search_keywords jsonb DEFAULT '[]'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SET search_path TO 'public', 'pg_catalog'
AS $function$
DECLARE
    v_target public.targets%ROWTYPE;
    v_link   public.user_targets%ROWTYPE;
BEGIN
    INSERT INTO public.targets AS t (
        label, description, normalized_label, scoring_profile, search_keywords,
        activation_status
    )
    VALUES (
        p_label, p_description, p_normalized_label, p_scoring_profile,
        p_search_keywords, COALESCE(p_activation_status, 'idle')
    )
    ON CONFLICT (normalized_label) DO UPDATE
        -- Only the lifecycle column, never the shared content. Matches the old
        -- ignore-duplicates + targeted-update pair.
        SET activation_status = COALESCE(p_activation_status, t.activation_status),
            updated_at = now()
    RETURNING * INTO v_target;

    IF v_target.id IS NULL THEN
        -- Unreachable with DO UPDATE (it always returns a row), but a silent
        -- NULL here would produce a membership pointing at nothing.
        RAISE EXCEPTION
            'create_target_and_link: no target row for normalized_label %',
            p_normalized_label;
    END IF;

    INSERT INTO public.user_targets (user_id, target_id, is_active, updated_at)
    VALUES (p_user_id, v_target.id, false, now())
    ON CONFLICT (user_id, target_id) DO UPDATE SET updated_at = now()
    RETURNING * INTO v_link;

    RETURN jsonb_build_object(
        'target', to_jsonb(v_target),
        'user_target', to_jsonb(v_link)
    );
END;
$function$;

COMMENT ON FUNCTION public.create_target_and_link(uuid, text, text, text, text, jsonb, jsonb) IS
  'Find-or-create a shared target and link the user to it in ONE transaction '
  '(#667). Removes the window in which a target exists with no membership — the '
  'state that made an orphan indistinguishable from a row mid-creation.';
