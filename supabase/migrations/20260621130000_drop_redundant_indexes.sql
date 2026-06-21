-- #2 audit (index fit): drop redundant indexes confirmed unused by LIVE pg_stat
-- (~1 month of production stats). Static analysis flagged ~12 "redundant"
-- indexes, but the live scan counts OVERTURNED most of them — e.g. idx_jts_job
-- (187k scans), idx_user_targets_user (534), idx_experience_optimized_user_version
-- (319) are actively used and are KEPT. Only the 5 below are BOTH structurally
-- redundant AND unused. DROP INDEX keeps row data (not destructive per #109) and
-- is unguarded by the #112 index-lock check; IF EXISTS makes it re-runnable.

-- Exact duplicate of the unique constraint index user_profiles_email_key (both
-- on (email)); the unique index serves the same email lookups (the 3 scans this
-- one took resolve identically against the unique).
DROP INDEX IF EXISTS public.idx_user_profiles_email;

-- Prefix-redundant, idx_scan = 0 over the stats window (covering index in [ ]):
--   idx_job_analyses_job_id (job_posting_id)  [idx_job_analyses_cache_key / idx_analyses_cache_lookup]
DROP INDEX IF EXISTS public.idx_job_analyses_job_id;
--   idx_tailored_resumes_job (job_posting_id) [idx_documents_job_doctype (job_posting_id, document_type)]
DROP INDEX IF EXISTS public.idx_tailored_resumes_job;
--   idx_job_notification_sent_user (user_profile_id) [unique (user_profile_id, job_posting_id, channel)
--     + idx_notifications_sent_user_channel_sent (user_profile_id, channel, sent_at DESC)]
DROP INDEX IF EXISTS public.idx_job_notification_sent_user;

-- Legacy denormalized jobs.score index, idx_scan = 0 — score reads go through
-- the scores table, not jobs.score. (jobs is a hot table, but DROP INDEX takes
-- only a brief metadata-level ACCESS EXCLUSIVE lock — milliseconds at current
-- scale — not the long SHARE lock a CREATE INDEX build would.)
DROP INDEX IF EXISTS public.idx_job_postings_score;
