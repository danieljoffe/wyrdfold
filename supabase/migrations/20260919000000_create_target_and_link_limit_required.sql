-- #1084 review: close the last uncapped active create.
--
-- In plain terms: the previous migration let a caller ask for an ACTIVE
-- membership while passing no cap at all, and the function would create the
-- target and activate the membership without checking the plan cap. No
-- application code did that, but the shape was expressible. Now an active
-- request must carry a limit, or the function refuses before it writes
-- anything, exactly as activate_user_target already does.
--
-- Forward migration rather than an edit: 20260918170000 is already applied on
-- production and staging, so its file cannot change what those databases run.
-- Everything else about the function is byte-for-byte the previous version.

CREATE OR REPLACE FUNCTION public.create_target_and_link(
    p_user_id uuid,
    p_label text,
    p_normalized_label text,
    p_activation_status text DEFAULT NULL,
    p_description text DEFAULT NULL,
    p_scoring_profile jsonb DEFAULT '{}'::jsonb,
    p_search_keywords jsonb DEFAULT '[]'::jsonb,
    p_is_active boolean DEFAULT false,
    p_active_limit integer DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SET search_path TO 'public', 'pg_catalog'
AS $function$
DECLARE
    v_target       public.targets%ROWTYPE;
    v_link         public.user_targets%ROWTYPE;
    v_target_id    uuid;
    v_was_created  boolean;
    v_active_count integer;
BEGIN
    IF p_is_active AND p_active_limit IS NULL THEN
        -- No uncapped active create exists. Refused before any write, so a
        -- rejected call leaves neither a target nor a membership behind.
        RAISE EXCEPTION 'create_target_and_link: p_active_limit is required when p_is_active'
            USING ERRCODE = 'PT400',
                  DETAIL  = '{"error": "LIMIT_REQUIRED"}';
    END IF;

    IF p_is_active THEN
        -- The one lock every active write serializes on (20260918170000).
        PERFORM pg_advisory_xact_lock(hashtext('user_targets_ceiling:' || p_user_id::text));
    END IF;

    INSERT INTO public.targets AS t (
        label, description, normalized_label, scoring_profile, search_keywords,
        activation_status
    )
    VALUES (
        p_label, p_description, p_normalized_label, p_scoring_profile,
        p_search_keywords, COALESCE(p_activation_status, 'idle')
    )
    ON CONFLICT (normalized_label) DO UPDATE
        -- Only the lifecycle column, never the shared content, and only when
        -- the caller asked for a status (a NULL leaves the row untouched).
        SET activation_status = COALESCE(p_activation_status, t.activation_status),
            updated_at = now()
    RETURNING t.id, (t.xmax = 0) INTO v_target_id, v_was_created;

    IF v_target_id IS NULL THEN
        RAISE EXCEPTION
            'create_target_and_link: no target row for normalized_label %',
            p_normalized_label;
    END IF;

    SELECT * INTO v_target FROM public.targets WHERE id = v_target_id;

    IF p_is_active THEN
        -- Re-activating a link the user already holds active changes no count,
        -- so it is exempt (the app's advisory check has the same rule).
        SELECT count(*) INTO v_active_count
          FROM public.user_targets
         WHERE user_id = p_user_id
           AND is_active
           AND target_id <> v_target_id;
        IF v_active_count >= p_active_limit THEN
            RAISE EXCEPTION 'active target limit reached (% of %)', v_active_count, p_active_limit
                USING ERRCODE = 'PT409',
                      DETAIL  = jsonb_build_object(
                                    'error', 'ACTIVE_LIMIT',
                                    'active_count', v_active_count,
                                    'limit', p_active_limit
                                )::text,
                      HINT    = 'Deactivate a target first.';
        END IF;
    END IF;

    INSERT INTO public.user_targets (user_id, target_id, is_active, updated_at)
    VALUES (p_user_id, v_target_id, p_is_active, now())
    ON CONFLICT (user_id, target_id) DO UPDATE
        -- An active request activates an existing inactive link; an inactive
        -- request (legacy shape) never deactivates one.
        SET is_active  = public.user_targets.is_active OR EXCLUDED.is_active,
            updated_at = now()
    RETURNING * INTO v_link;

    RETURN jsonb_build_object(
        'target',      to_jsonb(v_target),
        'user_target', to_jsonb(v_link),
        'was_created', v_was_created
    );
END;
$function$;

COMMENT ON FUNCTION public.create_target_and_link(uuid, text, text, text, text, jsonb, jsonb, boolean, integer) IS
  'Find-or-create a shared target and link the user to it in ONE transaction '
  '(#667). With p_is_active the link is active and the active-target cap '
  '(p_active_limit, REQUIRED: PT400 LIMIT_REQUIRED otherwise, before any '
  'write) is enforced in the same transaction under the per-user ceiling '
  'lock, raising PT409 with a JSON DETAIL {error, active_count, limit}; a '
  'rejection rolls the target insert back. was_created reports insert vs '
  'conflict so a caller that lost the exact-key race never treats the row as '
  'its own (#1071).';

NOTIFY pgrst, 'reload schema';
