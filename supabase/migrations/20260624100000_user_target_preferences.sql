-- Per-user target preferences (#60).
--
-- Targets are SHARED and the fit score is shared + cached; preferences are a
-- per-user READ-TIME filter/re-rank layered on top of the same scores. They
-- live on the user_targets junction (alongside job_score_threshold /
-- sms_score_threshold from #178 and axis_weights from PR E) so each user can
-- shape THEIR view of a shared target without forking the target or triggering
-- any per-user re-grading/re-scoring.
--
-- Defaults are chosen to be behaviorally neutral for existing rows: a 40-point
-- score floor (the same soft default the list view already nudges toward),
-- remote allowed, unknown salary kept. NULL array columns mean "no location /
-- employment-type preference" — the read path treats absence as "keep
-- everything", so existing rows are unaffected until a user opts in.
--
-- Notes on the seniority / employment-type columns: the JOB-side firewall tag
-- columns these filter against (jobs.employment_type / jobs.seniority /
-- jobs.metro / jobs.is_remote) are added by a separate firewall PR and are not
-- backfilled. The read path feature-detects them and treats a missing/NULL job
-- tag as "unknown → keep" (lenient), so this migration is safe to ship ahead
-- of that work. These columns store the user's *desired* range/set, free-text
-- so they don't couple to a job-side enum that doesn't exist yet.
ALTER TABLE public.user_targets
  ADD COLUMN IF NOT EXISTS pref_score_cutoff smallint DEFAULT 40
    CHECK (pref_score_cutoff BETWEEN 0 AND 200),
  ADD COLUMN IF NOT EXISTS pref_locations text[],
  ADD COLUMN IF NOT EXISTS pref_remote_ok boolean DEFAULT true,
  ADD COLUMN IF NOT EXISTS pref_seniority_min text,
  ADD COLUMN IF NOT EXISTS pref_seniority_max text,
  ADD COLUMN IF NOT EXISTS pref_employment_types text[],
  ADD COLUMN IF NOT EXISTS pref_include_unknown_salary boolean DEFAULT true;
