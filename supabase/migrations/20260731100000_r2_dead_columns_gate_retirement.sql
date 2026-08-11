-- R2 PR-b (schema audit Groups A/B/E — docs/decisions.md 2026-07-30 entries):
-- drop the vestigial single-target-era columns on jobs, the write-only
-- ledger columns, and the retired prescan gate's calibration column.
--
-- NOT backward-compatible with pre-R2 API code (old code inserts/selects
-- some of these) — ships in the R2 tight-cutover release window together
-- with the rename migration that follows it.
--
-- Evidence (verified 2026-07-30/31, prod):
--   jobs.target_id           non-null on 0/34,903 rows; no reader/writer.
--   jobs.score               live writer, sole reader was the retired
--                            legacy Stage-3 blend gate (phase2_enabled
--                            replaced it; zero poll_scoring llm_costs rows).
--   jobs.score_breakdown     write-once '{}'; every read overlaid from scores.
--   jobs.llm_score           402 rows; stale legacy-blend output.
--   jobs.llm_analysis_id     single global slot on a shared row —
--                            last-writer-wins across users; never SELECTed.
--   jobs.url_validation_*    write-only ledger of a live step (the step's
--                            absolute_url effects remain in code);
--                            0 'rejected' / 35k 'valid' ever.
--   scores.matched_keywords  write-only (36k rows); breakdown JSONB already
--                            embeds match components.
--   targets.prescan_cosine_threshold  the retired #90 gate's per-target
--                            calibration; gate removed from code in this PR
--                            (outcome-join: armed threshold dropped 61.5% of
--                            promising — "retired by its own shadow data").

-- guarded-destructive: every dropped object was verified dead against BOTH
-- prod data and a full code trace (audit doc + docs/decisions.md 2026-07-30):
-- target_id all-NULL (0/34,903); score/score_breakdown recomputable noise
-- (breakdown is write-once '{}'); url_validation_* 100% 'valid' ledger;
-- matched_keywords derivable from score_breakdown JSONB; prescan threshold
-- already NULLed by 20260730230000. The only real data lost — llm_score
-- (402 rows) / llm_analysis_id (42 rows) — is legacy-blend provenance
-- re-derivable from analyses(job_posting_id, user_id, created_at); a one-off
-- pre-apply snapshot of those two columns is taken at prod apply time per
-- the release runbook (schema-audit ship sequence, R2 cutover step).

-- ---- jobs: single-target era --------------------------------------------
ALTER TABLE public.jobs DROP CONSTRAINT IF EXISTS job_postings_target_id_fkey;
DROP INDEX IF EXISTS public.idx_job_postings_target;
ALTER TABLE public.jobs DROP COLUMN IF EXISTS target_id;

ALTER TABLE public.jobs DROP COLUMN IF EXISTS score;
ALTER TABLE public.jobs DROP COLUMN IF EXISTS score_breakdown;

DROP INDEX IF EXISTS public.idx_job_postings_llm_score;
ALTER TABLE public.jobs DROP COLUMN IF EXISTS llm_score;

ALTER TABLE public.jobs DROP CONSTRAINT IF EXISTS job_postings_llm_analysis_id_fkey;
DROP INDEX IF EXISTS public.idx_jobs_llm_analysis;
ALTER TABLE public.jobs DROP COLUMN IF EXISTS llm_analysis_id;

-- ---- write-only ledgers ---------------------------------------------------
ALTER TABLE public.jobs DROP COLUMN IF EXISTS url_validation_status;
ALTER TABLE public.jobs DROP COLUMN IF EXISTS url_validation_warnings;
ALTER TABLE public.scores DROP COLUMN IF EXISTS matched_keywords;

-- ---- prescan gate retirement ----------------------------------------------
ALTER TABLE public.targets DROP COLUMN IF EXISTS prescan_cosine_threshold;

-- ---- RPCs whose only callers died with the legacy Stage-3 blend ------------
DROP FUNCTION IF EXISTS public.get_job_scores_by_ids("p_ids" jsonb);
DROP FUNCTION IF EXISTS public.bulk_update_scores("p_updates" jsonb);

-- ---- user_apply_score_blend: keep the DB-enforced authz + scores write; ----
-- the jobs.llm_analysis_id stamp dies with the column. Body mirrors the LIVE
-- prod definition (uuid comparison — the 20260621140000 file predates the
-- text→uuid user_id migration) and the grants mirror the 20260718010000
-- lockdown: service_role ONLY (the authenticated grant was revoked when
-- scores writes moved fully behind the service client; the privilege
-- invariant test pins this).
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

    -- p_analysis_id retained in the signature for caller compatibility; the
    -- reverse pointer the old jobs.llm_analysis_id stamp provided is
    -- derivable from analyses(job_posting_id, user_id, created_at) (R2).
END;
$$;

ALTER FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid)
  FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid)
  TO service_role;

-- ---- user_upsert_score: its INSERT column list named matched_keywords. ----
-- Re-issue without it; body otherwise mirrors the live definition, grants
-- stay service_role-only (20260718010000 lockdown).
CREATE OR REPLACE FUNCTION public.user_upsert_score(p_row jsonb)
 RETURNS SETOF scores
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
        job_posting_id, target_id, score, score_breakdown,
        excluded, scoring_status, scored_profile_version, recency_score, updated_at
    ) VALUES (
        (p_row->>'job_posting_id')::uuid,
        v_target,
        (p_row->>'score')::int,
        p_row->'score_breakdown',
        (p_row->>'excluded')::boolean,
        p_row->>'scoring_status',
        (p_row->>'scored_profile_version')::int,
        (p_row->>'recency_score')::int,
        (p_row->>'updated_at')::timestamptz
    )
    ON CONFLICT (job_posting_id, target_id) DO UPDATE SET
        score = EXCLUDED.score,
        score_breakdown = EXCLUDED.score_breakdown,
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
REVOKE ALL ON FUNCTION public.user_upsert_score(jsonb) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.user_upsert_score(jsonb) TO service_role;
