-- Hardening review 2026-07-21 (DB-F3): restore DB-side bounds on
-- client-writable status/text columns that the jobs -> user_jobs migration lost.
--
-- The predecessor jobs.status carried jobs_status_check (a value enum), dropped
-- with the column in the user_jobs cutover. Its replacement — user_jobs.status
-- (text, no CHECK) — plus status_log.new_status/old_status/note and
-- job_feedback.reason are all writable by `authenticated` via a FOR ALL own-row
-- RLS policy, and the anon key ships in the browser. The FastAPI Pydantic gate
-- (Literal[...] status, note max_length=1000) binds only the API path; a direct
-- PostgREST PATCH bypasses it. Consequences today:
--   * user_jobs.status='<garbage>' -> pipeline_counts GROUP BY invents unknown
--     buckets (silent frontend miscounts);
--   * unbounded multi-MB writes into status/note/reason -> storage + index
--     bloat on a small, 57014-prone instance.
--
-- These restore parity with the pre-cutover schema. Prod verified 0 rows out of
-- range for every column before adding (2026-07-21), so each validates instantly.

ALTER TABLE public.user_jobs
  ADD CONSTRAINT user_jobs_status_check
  CHECK (status IN (
    'new','saved','resume_draft','resume_ready',
    'applied','interviewing','offer','rejected','archived'
  ));

ALTER TABLE public.status_log
  ADD CONSTRAINT status_log_len_check
  CHECK (
    char_length(new_status) <= 32
    AND (old_status IS NULL OR char_length(old_status) <= 32)
    AND (note IS NULL OR char_length(note) <= 1000)
  );

ALTER TABLE public.job_feedback
  ADD CONSTRAINT job_feedback_reason_len_check
  CHECK (reason IS NULL OR char_length(reason) <= 2000);
