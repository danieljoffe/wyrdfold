-- #6 R2 (step 2): DB-enforced authorization for the manual-add scores writes.
--
-- POST /jobs/manual scores a user-pasted job against the caller's active
-- targets, writing per-(job, target) `scores` rows + force-including them
-- (excluded=false). The target set is already scoped to the caller in Python
-- (#24 F4), but the writes ran via the service-role client. Move them behind
-- SECURITY DEFINER RPCs so Postgres re-checks target ownership — the same
-- pattern as user_apply_score_blend (#6 R2 step 1). Called on the caller's
-- client: a JWT user is scoped to targets they follow; service-role
-- (auth.uid() NULL = operator/poller) is exempt. The background poller keeps
-- writing via the un-gated path (`_upsert_score` default).
--
-- Grants: authenticated + service_role only; PUBLIC revoked (functions get a
-- PUBLIC EXECUTE by default — see 20260621120000).

-- Gated upsert of one scores row. Mirrors app.services.target_scoring._upsert_score's
-- column set; leaves promising / phase1_confidence untouched on conflict (the
-- manual-add path never sets them). Returns the row so the caller can parse it.
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
        WHERE user_id = (SELECT auth.uid())::text
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

ALTER FUNCTION public.user_upsert_score(jsonb) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.user_upsert_score(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.user_upsert_score(jsonb) TO authenticated, service_role;

-- Gated force-include: set excluded=false for a job across the caller's own
-- targets (the manual-add "you pasted this, so show it" override, #24 F4). All
-- target ids must be owned by the caller (service-role exempt).
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
            WHERE user_id = (SELECT auth.uid())::text
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

ALTER FUNCTION public.user_set_scores_included(uuid, uuid[]) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.user_set_scores_included(uuid, uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.user_set_scores_included(uuid, uuid[]) TO authenticated, service_role;
