-- Hardening review 2026-07-21 (DB-F6): close the denorm-sync race that can pin
-- job_is_live=true on a job that has just been archived / flipped non-US.
--
-- Two triggers keep scores.job_is_live (etc.) in sync with the parent job
-- (20260717040000): scores_sync_denorm reads the parent on a scores INSERT;
-- jobs_sync_scores_denorm fans a jobs change out to its scores rows. Both read
-- under their own snapshot with no lock, so a scores INSERT (poller grading,
-- POST /jobs/manual upsert) that overlaps a jobs archival/is_us-flip can miss
-- each other: the new scores row commits with a stale job_is_live=true, enters
-- idx_scores_live_dedup, and nothing corrects it — plain re-scores skip the
-- lookup and the jobs trigger only fires on the NEXT jobs change. The result is
-- an archived/non-US job showing as live in get_cross_target_jobs (a #257-class
-- ghost), silent and sticky.
--
-- Fix: take a FOR SHARE lock on the parent job row during the INSERT-time
-- populate. That serializes against the jobs UPDATE (which needs an exclusive
-- lock on the same row): whichever commits first, the other then sees the
-- committed state — either the scores read waits and reads the archived values,
-- or the jobs fan-out waits and then sees (and corrects) the new scores row.
--
-- The lock is scoped to TG_OP='INSERT' on purpose. On INSERT the new tuple is
-- not yet in the heap during the BEFORE trigger, so this txn holds no scores-row
-- lock while it waits for the job row — no lock cycle with the jobs->scores
-- fan-out. The legacy `job_is_live IS NULL` recovery branch (a pre-backfill row
-- incidentally updated; none exist post-20260718030000) keeps its unlocked read:
-- locking there would invert the lock order (scores row already locked by the
-- UPDATE, then wait on the job row) and could deadlock with the fan-out. That
-- branch is best-effort self-healing anyway.

CREATE OR REPLACE FUNCTION public.scores_sync_denorm()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
BEGIN
    -- Gradedness is a same-row property (matches the RPC's axis_scores IS NOT NULL).
    NEW.is_graded := (NEW.axis_scores IS NOT NULL);
    -- Populate the jobs-derived columns from the parent job. On INSERT, take a
    -- FOR SHARE lock so a concurrent jobs archival/is_us-flip can't leave this
    -- row's job_is_live stale (DB-F6). Plain re-scores / recency sweeps skip the
    -- lookup: the job's liveness/family/first_seen don't change under a scores
    -- UPDATE (the jobs trigger owns those).
    IF TG_OP = 'INSERT' THEN
        SELECT (jp.archived_at IS NULL AND jp.purged_at IS NULL AND jp.is_us IS NOT FALSE),
               jp.role_family, jp.first_seen_at
          INTO NEW.job_is_live, NEW.job_role_family, NEW.job_first_seen_at
          FROM public.jobs jp
         WHERE jp.id = NEW.job_posting_id
         FOR SHARE;
    ELSIF NEW.job_is_live IS NULL THEN
        -- Legacy pre-backfill recovery: no lock (see header — avoids inverting
        -- the lock order against the jobs->scores fan-out).
        SELECT (jp.archived_at IS NULL AND jp.purged_at IS NULL AND jp.is_us IS NOT FALSE),
               jp.role_family, jp.first_seen_at
          INTO NEW.job_is_live, NEW.job_role_family, NEW.job_first_seen_at
          FROM public.jobs jp
         WHERE jp.id = NEW.job_posting_id;
    END IF;
    RETURN NEW;
END;
$$;

-- Trigger binding unchanged (BEFORE INSERT OR UPDATE ON scores, via REPLACE).
