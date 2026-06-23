-- Phase-3 analysis cache: enforce one analyses row per cache key.
--
-- ROOT CAUSE this fixes: the user-facing ``POST /analysis`` flow and the
-- cron poller (Stage 3) both write the ``analyses`` cache, but the poller
-- stamped ``user_id = NULL`` while computing against each user's OWN
-- optimized doc. The user view reads under its real ``user_id`` (the JWT
-- ``sub``), so it never matched the poller's NULL-owned row for the same
-- (job, target, optimized_doc_id) — every first visit re-fired a full
-- LLM analysis the cron had already paid for. The poller now stamps
-- ``optimized_doc.user_id`` (the app fix); this constraint makes the
-- one-row-per-cache-key invariant a DB-level guarantee and gives the
-- idempotent upsert in persistence.persist() a conflict target.
--
-- There was previously NO uniqueness on the cache key (only a plain
-- ``idx_job_analyses_cache_key`` btree), so duplicate rows could and did
-- accumulate. We de-dup before adding the constraint.

-- 1. Collapse existing duplicates: keep the newest row per cache key,
--    delete the rest. NULL user_id rows are grouped together (the
--    coalesce mirrors the NULLS NOT DISTINCT semantics below) so legacy
--    poller rows dedup against each other. Re-point jobs.llm_analysis_id
--    away from any row we're about to delete, onto the surviving newest
--    row for the same (job, target, optimized, owner) key, so the FK
--    (ON DELETE SET NULL) doesn't blank out a still-valid analysis link.
WITH ranked AS (
    SELECT
        id,
        job_posting_id,
        target_id,
        optimized_doc_id,
        user_id,
        row_number() OVER (
            PARTITION BY
                job_posting_id,
                target_id,
                optimized_doc_id,
                COALESCE(user_id::text, '__null__')
            ORDER BY created_at DESC, id DESC
        ) AS rn
    FROM public.analyses
),
survivors AS (
    SELECT id, job_posting_id, target_id, optimized_doc_id, user_id
    FROM ranked
    WHERE rn = 1
),
losers AS (
    SELECT r.id, s.id AS survivor_id
    FROM ranked r
    JOIN survivors s
      ON r.job_posting_id = s.job_posting_id
     AND r.target_id = s.target_id
     AND r.optimized_doc_id IS NOT DISTINCT FROM s.optimized_doc_id
     AND r.user_id IS NOT DISTINCT FROM s.user_id
    WHERE r.rn > 1
)
UPDATE public.jobs j
SET llm_analysis_id = l.survivor_id
FROM losers l
WHERE j.llm_analysis_id = l.id;

DELETE FROM public.analyses a
USING (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY
                job_posting_id,
                target_id,
                optimized_doc_id,
                COALESCE(user_id::text, '__null__')
            ORDER BY created_at DESC, id DESC
        ) AS rn
    FROM public.analyses
) d
WHERE a.id = d.id
  AND d.rn > 1;

-- 2. Enforce one row per cache key. NULLS NOT DISTINCT (Postgres 15+; this
--    project runs PG17) so the legacy ``user_id IS NULL`` rows are also
--    deduplicated/guarded — without it, NULLs would each be treated as a
--    distinct value and the constraint would be toothless for them.
ALTER TABLE public.analyses
    ADD CONSTRAINT analyses_cache_key_unique
    UNIQUE NULLS NOT DISTINCT
    (job_posting_id, target_id, optimized_doc_id, user_id);

-- The unique constraint's backing index supersedes the non-unique
-- idx_job_analyses_cache_key (same leading columns), so drop the
-- redundant one.
DROP INDEX IF EXISTS public.idx_job_analyses_cache_key;
