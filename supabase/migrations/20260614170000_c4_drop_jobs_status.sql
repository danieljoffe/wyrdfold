-- C4 (#75): jobs.status is fully replaced (per-user status -> user_jobs,
-- global liveness -> jobs.archived_at). Drop the legacy column. First
-- recreate the url-health-due partial index, whose predicate referenced
-- status, to key on archived_at instead.
DROP INDEX IF EXISTS public.jobs_url_health_due_idx;
CREATE INDEX jobs_url_health_due_idx
  ON public.jobs ("last_url_check_at" NULLS FIRST)
  WHERE archived_at IS NULL;

-- Dropping the column auto-drops the dependent objects:
-- idx_job_postings_status, idx_jobs_status_score, jobs_status_check.
ALTER TABLE public.jobs DROP COLUMN status;
