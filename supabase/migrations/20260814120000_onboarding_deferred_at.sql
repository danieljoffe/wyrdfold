-- Third onboarding state: DEFERRED (docs/onboarding-sweep-2026-08-14.md P1/A1).
--
-- The wizard's global exits ("Skip setup for now", "Finish setup later") used to
-- POST /onboarding/complete, because the dashboard gate redirects any
-- completed_at IS NULL profile back into the wizard — without a third state,
-- not-completing meant being trapped (the old "skip doesn't stick" loop). The
-- side effect: "Finish setup later" was permanent — /onboarding bounced to the
-- dashboard forever and mid-flow resume (#85) only ever helped users who closed
-- the tab instead of clicking the labeled exit.
--
-- onboarding_deferred_at records "user deliberately exited the wizard without
-- finishing":
--   * dashboard gate redirects to /onboarding only when completed_at AND
--     deferred_at are both NULL;
--   * /onboarding itself keeps admitting (and resuming) any profile with
--     completed_at IS NULL, so the deferred user can genuinely come back later;
--   * POST /onboarding/complete and /onboarding/reset both clear it.
--
-- Additive + nullable: safe to apply before the code deploy. Existing rows keep
-- NULL — users who exited under the old wiring have completed_at set and are
-- unaffected.

alter table public.user_profiles
  add column if not exists onboarding_deferred_at timestamptz;

comment on column public.user_profiles.onboarding_deferred_at is
  'Set when the user deliberately exits the onboarding wizard without finishing '
  '("Finish setup later"). Suppresses the dashboard''s auto-redirect into the '
  'wizard while keeping /onboarding enterable + resumable. Cleared by '
  'onboarding complete and reset.';
