-- Trial tier (#841) — a new account must be able to USE the product before
-- it pays.
--
-- WHY THIS EXISTS. `plan` defaulted to 'free', and free is BYOK
-- (`entitlements_for('free').llm_key_source == 'byok'`). In saas mode that
-- means `llm.get_client_async` raises MissingUserKeyError for every free
-- account — a 402 telling the user to add an OpenRouter key. This
-- deployment has no `BYOK_MASTER_KEY`, so `/settings` correctly reports
-- that bring-your-own-key is unavailable. The error instructed the user to
-- do the one thing the app said it could not do, and onboarding dead-ended
-- at step 2 of 4. See #841.
--
-- 'trial' is a HOST-key plan (so onboarding works) bounded by BOTH a total
-- spend ceiling and a duration. Unlike starter/pro it counts BACKGROUND
-- purposes against its ceiling: a paid tier excludes them because a
-- subscription is already paying for them, whereas an idle trial account
-- polling a target for days has no subscription behind it. The ceiling
-- would otherwise bound only the cheap half.
--
-- ORDER MATTERS, and this migration is safe to apply BEFORE the code
-- deploys (the repo's additive-before-merge rule):
--   1. widen the CHECK first, or step 3's backfill violates it;
--   2. add `trial_started_at` with a now() default so EVERY profile-creation
--      path is stamped — the API creates rows lazily in more than one place,
--      and a default cannot be forgotten by a new call site;
--   3. backfill existing 'free' rows to 'trial'. Under the OLD code 'trial'
--      is an unknown plan and `entitlements_for` falls through to free/BYOK
--      — i.e. exactly the behaviour those accounts have today, so the
--      backfill is a no-op until the new code lands. Under the NEW code they
--      get a working trial instead of a wall.
--   4. only then flip the DEFAULT, so future signups start on trial.
--
-- Deliberately NOT touched: 'starter'/'pro' rows. A paying account must
-- never be moved onto a trial, and this migration must stay replay-safe.

-- 1. widen the constraint before anything writes 'trial'
ALTER TABLE public.user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_plan_check;
ALTER TABLE public.user_profiles
    ADD CONSTRAINT user_profiles_plan_check
    CHECK (plan IN ('free', 'trial', 'starter', 'pro'));

-- 2. when the trial clock started. Default now() so lazily-created profiles
--    are stamped without the application having to remember.
--
--    NULL is tolerated by the code and treated as "not expired" rather than
--    "expired": a missing stamp is a data anomaly, and failing closed would
--    wall a legitimate user, while failing open is still bounded by the
--    total spend ceiling (which is the real control — the duration exists to
--    stop BACKGROUND spend on an account that has stopped converting).
ALTER TABLE public.user_profiles
    ADD COLUMN IF NOT EXISTS trial_started_at timestamptz DEFAULT now();

COMMENT ON COLUMN public.user_profiles.trial_started_at IS
  'When this account''s trial clock started. Read with plan = ''trial'' to '
  'decide expiry; NULL means "unknown, treat as not expired" — the total '
  'spend ceiling still bounds the account. Ignored for other plans.';

-- 3. existing free accounts get a trial rather than staying walled.
--    Stamped to now() so everyone gets the full window from the cutover,
--    not a window that silently already expired.
UPDATE public.user_profiles
   SET plan = 'trial',
       trial_started_at = now()
 WHERE plan = 'free';

-- 4. future signups start on trial
ALTER TABLE public.user_profiles
    ALTER COLUMN plan SET DEFAULT 'trial';
