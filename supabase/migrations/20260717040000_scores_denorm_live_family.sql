-- perf(#365 floor): denormalize jobs' live/family/first-seen + gradedness onto
-- scores so the cross-target dedup, off-family gate, and score sort run
-- INDEX-ONLY — reading `jobs` only for the final <=limit page.
--
-- WHY. EXPLAIN (ANALYZE, BUFFERS) on the heaviest user (4 targets, 19,065 live
-- scores) showed get_cross_target_jobs touches ~18,900 buffer pages to render 6
-- rows: a 13,951-page scores index-only scan (wide, because axis_scores JSONB is
-- in the covering index) over ALL 19k rows, plus a 4,956-page hash of the 4,963
-- live jobs for the live filter. Warm those are cache hits (~156ms); cold (the
-- poller churns 148MB out of cache on the small instance) they are disk reads
-- (~5.4s). Shrinking the working set ~100x is the durable fix for the cold floor.
--
-- WHAT. Four denormalized columns on scores, kept in sync by triggers:
--   * is_graded            = (axis_scores IS NOT NULL)      -- same-row
--   * job_is_live          = archived_at IS NULL AND purged_at IS NULL AND is_us IS NOT FALSE
--   * job_role_family      = jobs.role_family
--   * job_first_seen_at    = jobs.first_seen_at
-- plus a PARTIAL, COMPACT covering index over only live+non-excluded rows with no
-- JSONB — so the next migration's rewritten RPC scans ~3,351 live rows index-only
-- and never reads the scores heap or the jobs table until the 6-row page.
--
-- SYNC. is_graded and the three jobs-derived columns are maintained by:
--   1. scores_sync_denorm (BEFORE INSERT OR UPDATE on scores): always recomputes
--      is_graded from the row's own axis_scores; populates the jobs-derived cols
--      from the parent job on INSERT (or when still NULL — e.g. a not-yet-
--      backfilled row touched by the recency sweep). One indexed job lookup per
--      INSERT (a cache hit at scoring time); no lookup on plain UPDATEs.
--   2. jobs_sync_scores_denorm (AFTER UPDATE on jobs, only when a live-relevant
--      column changes — rare: archival, re-tag, is_us flip): fans the new values
--      out to that job's (few) scores rows. Bounded fan-out; fires ~never in
--      steady state since these columns are set once at ingestion.
-- first_seen_at is immutable post-ingestion, so the sort key can't go stale.
--
-- EXISTING ROWS. Fresh inserts (tests, CI, new prod rows) are trigger-populated,
-- so no backfill lives here. Prod's ~30k pre-existing rows are backfilled by a
-- separate THROTTLED script BEFORE the RPC (next migration) goes live — until
-- then their job_is_live is NULL, so they are simply absent from the partial
-- index (and the old RPC, still live, does not read these columns).
--
-- index-lock-ok: new PARTIAL index on scores over only ~3.3k live rows — a fast
-- build. CONCURRENTLY can't run inside the txn `supabase db push` wraps each file
-- in (#112); a brief write lock is acceptable at this scale in a poll-quiet
-- window, and the poller retries transient write failures. The two CREATE TRIGGER
-- statements take a brief SHARE ROW EXCLUSIVE lock each — same acceptable window.

ALTER TABLE public.scores
    ADD COLUMN IF NOT EXISTS is_graded boolean,
    ADD COLUMN IF NOT EXISTS job_is_live boolean,
    ADD COLUMN IF NOT EXISTS job_role_family text,
    ADD COLUMN IF NOT EXISTS job_first_seen_at timestamp with time zone;

-- ---- sync 1: scores write -> keep this row's denormalized columns current ----
CREATE OR REPLACE FUNCTION public.scores_sync_denorm()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
BEGIN
    -- Gradedness is a same-row property (matches the RPC's axis_scores IS NOT NULL).
    NEW.is_graded := (NEW.axis_scores IS NOT NULL);
    -- Populate the jobs-derived columns from the parent job on INSERT, or when
    -- they are still NULL (a pre-backfill row incidentally updated). Plain
    -- re-scores / recency sweeps skip the lookup: the job's liveness/family/
    -- first_seen don't change under a scores UPDATE (the jobs trigger owns those).
    IF TG_OP = 'INSERT' OR NEW.job_is_live IS NULL THEN
        SELECT (jp.archived_at IS NULL AND jp.purged_at IS NULL AND jp.is_us IS NOT FALSE),
               jp.role_family, jp.first_seen_at
          INTO NEW.job_is_live, NEW.job_role_family, NEW.job_first_seen_at
          FROM public.jobs jp
         WHERE jp.id = NEW.job_posting_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER scores_sync_denorm_biu
    BEFORE INSERT OR UPDATE ON public.scores
    FOR EACH ROW
    EXECUTE FUNCTION public.scores_sync_denorm();

-- ---- sync 2: job liveness/family/first_seen change -> fan out to its scores ----
CREATE OR REPLACE FUNCTION public.jobs_sync_scores_denorm()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
BEGIN
    UPDATE public.scores s SET
        job_is_live = (NEW.archived_at IS NULL AND NEW.purged_at IS NULL AND NEW.is_us IS NOT FALSE),
        job_role_family = NEW.role_family,
        job_first_seen_at = NEW.first_seen_at
    WHERE s.job_posting_id = NEW.id
      AND (s.job_is_live IS DISTINCT FROM (NEW.archived_at IS NULL AND NEW.purged_at IS NULL AND NEW.is_us IS NOT FALSE)
           OR s.job_role_family IS DISTINCT FROM NEW.role_family
           OR s.job_first_seen_at IS DISTINCT FROM NEW.first_seen_at);
    RETURN NULL;
END;
$$;

CREATE TRIGGER jobs_sync_scores_denorm_au
    AFTER UPDATE ON public.jobs
    FOR EACH ROW
    WHEN (OLD.archived_at   IS DISTINCT FROM NEW.archived_at
       OR OLD.purged_at     IS DISTINCT FROM NEW.purged_at
       OR OLD.is_us         IS DISTINCT FROM NEW.is_us
       OR OLD.role_family   IS DISTINCT FROM NEW.role_family
       OR OLD.first_seen_at IS DISTINCT FROM NEW.first_seen_at)
    EXECUTE FUNCTION public.jobs_sync_scores_denorm();

-- Partial, compact covering index: only live+non-excluded rows, no JSONB. Lets
-- the rewritten RPC's dedup + off-family gate + score sort run index-only.
CREATE INDEX idx_scores_live_dedup ON public.scores
    (target_id, score DESC, job_posting_id DESC)
    INCLUDE (scoring_status, is_graded, job_role_family, job_first_seen_at)
    WHERE job_is_live AND NOT excluded;
