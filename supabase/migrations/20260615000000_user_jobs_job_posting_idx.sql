-- #108: cover user_jobs.job_posting_id.
-- The PK is (user_id, job_posting_id), so job_posting_id has no leading index:
--   - the FK job_posting_id -> jobs(id) ON DELETE CASCADE is uncovered, so job
--     delete/archive cascades seq-scan user_jobs; and
--   - the source_live_unengaged_jobs / due_url_health_jobs anti-joins
--     (NOT EXISTS (... uj.job_posting_id = j.id ...)) seq-scan on every poll cycle.
-- A full (not partial) index is required to also cover the FK cascade.
-- Plain (non-CONCURRENTLY) build to match the repo convention and the
-- txn-wrapped `supabase db push` path; the CONCURRENTLY strategy is tracked in #112.
CREATE INDEX IF NOT EXISTS idx_user_jobs_job_posting
  ON public.user_jobs (job_posting_id);
