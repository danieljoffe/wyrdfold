-- #665 prep: ``scores.recency_score`` must never be NULL.
--
-- WHY THIS EXISTS. #665 makes ``recency_score`` the ONE number the list filters,
-- sorts and displays. The column was nullable with no default, and
-- ``recency_score >= N`` evaluates to NULL for a NULL row — so any row that
-- missed the writer would be silently EXCLUDED from every floored list. Not
-- degraded, not surfaced: gone. Prod has 0 NULLs today, which is exactly what
-- makes that failure mode invisible right up until it isn't.
--
-- WHY AT THE WRITE SITE. COALESCEing on read is the obvious patch and the wrong
-- one. It must be repeated at every read site (two SQL functions, several
-- clauses each), and PostgREST — which the two-query list path filters through —
-- cannot express COALESCE at all. So the RPC would keep the row while the Python
-- path dropped it, and the same query would return different results depending
-- on which path served it. The RPC-vs-Python equivalence matrix in
-- ``tests/integration/test_cross_target_jobs_rpc.py`` catches exactly that, and
-- did: read-side COALESCE took the suite from 13 failures to 36.
--
-- Filling it once, in the trigger that already owns every other denormalised
-- column on this table, means every reader sees a real number and no read site
-- has to remember.
--
-- ``recency_score = score`` is the correct fallback: it is precisely what
-- ``compute_recency_score(score, age, enabled=False)`` returns, i.e. "not decayed
-- (yet)". The scheduler sweep overwrites it with the aged value on its next tick.

-- 1. Backfill anything already NULL (0 rows in prod at time of writing; this is
--    for fresh/staging DBs and for safety).
UPDATE public.scores SET recency_score = score WHERE recency_score IS NULL;

-- 2. Fill it on every future write, from the trigger that already computes the
--    other denorm columns. Body is the deployed definition verbatim plus the one
--    new line — keep it that way.
CREATE OR REPLACE FUNCTION public.scores_sync_denorm()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $function$
BEGIN
    NEW.is_graded := (NEW.axis_scores IS NOT NULL);
    -- #665: never NULL. A NULL here makes `recency_score >= N` evaluate NULL,
    -- which silently drops the row from every floored list. `score` is the
    -- honest stand-in for "not decayed yet" — it is what
    -- compute_recency_score(..., enabled=False) returns — and the scheduler
    -- sweep replaces it with the aged value on its next tick.
    NEW.recency_score := COALESCE(NEW.recency_score, NEW.score);
    IF TG_OP = 'INSERT' THEN
        SELECT (jp.archived_at IS NULL AND jp.purged_at IS NULL AND jp.is_us IS NOT FALSE),
               jp.role_family, COALESCE(jp.source_posted_at, jp.cataloged_at)
          INTO NEW.job_is_live, NEW.job_role_family, NEW.job_first_seen_at
          FROM public.jobs jp
         WHERE jp.id = NEW.job_posting_id
         FOR SHARE;
    ELSIF NEW.job_is_live IS NULL THEN
        SELECT (jp.archived_at IS NULL AND jp.purged_at IS NULL AND jp.is_us IS NOT FALSE),
               jp.role_family, COALESCE(jp.source_posted_at, jp.cataloged_at)
          INTO NEW.job_is_live, NEW.job_role_family, NEW.job_first_seen_at
          FROM public.jobs jp
         WHERE jp.id = NEW.job_posting_id;
    END IF;
    RETURN NEW;
END;
$function$;

-- 3. Now that nothing can write a NULL, make the schema say so. This is the
--    part that turns "true today" into "cannot become false".
ALTER TABLE public.scores ALTER COLUMN recency_score SET NOT NULL;
