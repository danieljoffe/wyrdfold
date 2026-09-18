-- #1071: one database contract for every write that makes a membership ACTIVE.
--
-- In plain terms: creating a target from a job posting becomes one database
-- step that either creates-and-attaches, or attaches to a target that already
-- exists, tells the caller which one happened, and refuses at the user's
-- active-target cap before anything is written. And every other way a target
-- becomes active for a user (activate, follow-as-active, the from-posting
-- attach) goes through one function that checks the cap under the same lock,
-- so two requests on two different screens can no longer both squeeze past a
-- cap of one. Until now the cap was checked in the application before each
-- write, with nothing serializing the check against the write.
--
-- Serialization contract: every cap-checked write takes the SAME per-user
-- transaction-scoped advisory lock the existing hard-ceiling trigger takes
-- (`enforce_active_target_ceiling`, key `'user_targets_ceiling:' || user_id`).
-- The trigger re-acquires it inside the same transaction, which is re-entrant,
-- so the plan cap (application-resolved, passed in) and the hard ceiling (25,
-- fixed in the trigger) are enforced under one lock with no ordering hazard.
--
--   * create_target_and_link gains p_is_active / p_active_limit and returns
--     was_created (insert vs conflict, xmax = 0). A cap rejection rolls the
--     target insert back, so no orphan target is ever visible; a caller that
--     lost the exact-key race gets was_created=false and must treat the row as
--     shared, never as its own to derive over. The conflict branch touches only
--     the lifecycle column, and only when the caller passes a status.
--   * activate_user_target is the one path for activating an existing or new
--     membership: lock, count the user's OTHER active memberships, refuse at
--     p_active_limit, upsert active (fit-score fields written only when
--     supplied, so a bare activation never blanks a stored score). A NULL limit
--     is "serialized but uncapped": reserved for restoring a link the same
--     request just deactivated (the activate-with-swap rollback).
--
-- Rejections raise SQLSTATE PT409, which PostgREST maps to HTTP 409; DETAIL
-- carries {error, active_count, limit} as JSON so the app builds the same
-- ACTIVE_LIMIT payload it sends today without parsing prose.
--
-- create_target_and_link's old seven-parameter signature is DROPPED and the
-- function recreated with defaults rather than overloaded: PostgREST resolves
-- rpc() by the keys present in the call, and two candidates that both accept
-- the legacy seven keys would be ambiguous (PGRST203). Legacy callers pass
-- neither new parameter and keep their exact behaviour: inactive link, no cap
-- check, conflict-update of the lifecycle column only.
--
-- SECURITY INVOKER (the default) on both, matching the existing function: the
-- only caller is the API's service role.

DROP FUNCTION IF EXISTS public.create_target_and_link(uuid, text, text, text, text, jsonb, jsonb);

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
    IF p_is_active THEN
        -- The one lock every active write serializes on (see header).
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

    IF p_is_active AND p_active_limit IS NOT NULL THEN
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
  '(p_active_limit) is enforced in the same transaction under the per-user '
  'ceiling lock, raising PT409 with a JSON DETAIL {error, active_count, '
  'limit}; a rejection rolls the target insert back. was_created reports '
  'insert vs conflict so a caller that lost the exact-key race never treats '
  'the row as its own (#1071).';


CREATE OR REPLACE FUNCTION public.activate_user_target(
    p_user_id uuid,
    p_target_id uuid,
    p_active_limit integer DEFAULT NULL,
    p_fit_score integer DEFAULT NULL,
    p_fit_score_reasoning text DEFAULT NULL,
    p_fit_score_prose_doc_id uuid DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SET search_path TO 'public', 'pg_catalog'
AS $function$
DECLARE
    v_link         public.user_targets%ROWTYPE;
    v_active_count integer;
BEGIN
    -- The one lock every active write serializes on (see header).
    PERFORM pg_advisory_xact_lock(hashtext('user_targets_ceiling:' || p_user_id::text));

    IF p_active_limit IS NOT NULL THEN
        -- The membership being activated is excluded from the count, so an
        -- idempotent re-activation of a link the user already holds active
        -- is exempt (same rule as the application's advisory check).
        SELECT count(*) INTO v_active_count
          FROM public.user_targets
         WHERE user_id = p_user_id
           AND is_active
           AND target_id <> p_target_id;
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

    INSERT INTO public.user_targets (
        user_id, target_id, is_active,
        fit_score, fit_score_reasoning, fit_score_prose_doc_id, updated_at
    )
    VALUES (
        p_user_id, p_target_id, true,
        p_fit_score, p_fit_score_reasoning, p_fit_score_prose_doc_id, now()
    )
    ON CONFLICT (user_id, target_id) DO UPDATE
        -- Fit-score fields are written only when supplied (the app's
        -- conditional-shape contract): a bare activation never blanks a
        -- stored score.
        SET is_active              = true,
            fit_score              = COALESCE(EXCLUDED.fit_score, public.user_targets.fit_score),
            fit_score_reasoning    = COALESCE(EXCLUDED.fit_score_reasoning, public.user_targets.fit_score_reasoning),
            fit_score_prose_doc_id = COALESCE(EXCLUDED.fit_score_prose_doc_id, public.user_targets.fit_score_prose_doc_id),
            updated_at             = now()
    RETURNING * INTO v_link;

    RETURN to_jsonb(v_link);
END;
$function$;

COMMENT ON FUNCTION public.activate_user_target(uuid, uuid, integer, integer, text, uuid) IS
  'The one path for making a membership ACTIVE (#1071): takes the per-user '
  'ceiling lock, counts the user''s other active memberships, refuses at '
  'p_active_limit with PT409 + JSON DETAIL {error, active_count, limit}, and '
  'upserts the link active in the same transaction. Fit-score fields are '
  'written only when supplied. A NULL limit is serialized but uncapped, for '
  'restoring a link the same request just deactivated.';

NOTIFY pgrst, 'reload schema';
