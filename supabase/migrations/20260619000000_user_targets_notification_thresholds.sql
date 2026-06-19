-- Per-target notification thresholds (#15).
--
-- Today both alert thresholds live on user_profiles (job_score_threshold for
-- email, sms_score_threshold for SMS) — one value per user across their whole
-- search. These per-target columns let a user with several active targets set
-- a different bar per target (e.g. ping me on any Staff FE >=70 but only top
-- Senior FE >=90).
--
-- NULL means "use the user-profile default", so existing rows are unaffected
-- and notify.py only diverges from the profile default once a target sets one.
-- The 0-200 bound matches the profile-level columns (scores can exceed 100
-- once category weights compound).
ALTER TABLE public.user_targets
  ADD COLUMN IF NOT EXISTS job_score_threshold integer
    CHECK (job_score_threshold BETWEEN 0 AND 200),
  ADD COLUMN IF NOT EXISTS sms_score_threshold integer
    CHECK (sms_score_threshold BETWEEN 0 AND 200);
