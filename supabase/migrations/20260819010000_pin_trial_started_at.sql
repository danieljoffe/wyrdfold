-- Pin the trial clock (#841 follow-up — found by the release-gate abuse probe).
--
-- 20260819000000 added `user_profiles.trial_started_at` and made it decide
-- whether a trial has expired. `protect_user_profiles_entitlements` pins the
-- other server-trusted columns (plan, llm_enabled, llm_monthly_budget_usd,
-- max_active_targets) but knew nothing about the new one, so a user hitting
-- PostgREST with their own token could PATCH their own trial forward:
--
--     before : trial_started_at = 30 days ago   (expired)
--     PATCH  : {"trial_started_at": "now"}      -> accepted
--     after  : trial_started_at = now           -> unlimited free trial
--
-- Reproduced against a local stack before this migration, blocked after.
--
-- PINNING ON UPDATE IS NOT ENOUGH. RLS lets a user DELETE their own
-- user_profiles row and INSERT a fresh one, which re-runs the INSERT branch
-- and would hand them a brand-new clock — an unlimited trial by another
-- route. So a user-client INSERT seeds an ALREADY-EXPIRED clock ('epoch'):
-- churning the row can never buy trial time.
--
-- Why 'epoch' and not '-infinity' or NULL: the API parses this column with
-- `entitlements.parse_trial_stamp`, which degrades an unreadable value to
-- None, and `trial_expired` treats None as NOT expired (deliberately — see
-- that function). '-infinity' does not survive `datetime.fromisoformat`, so
-- it would fail OPEN and grant exactly the unlimited trial this migration
-- exists to prevent. 'epoch' parses cleanly and is unambiguously expired.
--
-- Why not derive from `auth.users.created_at` (the first attempt): this
-- trigger is SECURITY INVOKER, so it runs as the caller, who has no SELECT on
-- `auth.users` — it made every user-client INSERT fail with "permission denied
-- for table users". Switching to SECURITY DEFINER to reach that table trades a
-- narrow cost hole for a privilege-escalation surface on the entitlement
-- guard itself. Not worth it: the app creates profiles with the SERVICE ROLE
-- (exempt below, and it gets the column DEFAULT now()), so the user-client
-- INSERT path is a hardening fallback that should land walled anyway.
--
-- The INSERT branch also stops hardcoding plan := 'free'. That predates the
-- trial tier; leaving it would mean any profile created through a user client
-- lands walled with the WRONG message ("add your OpenRouter key") instead of
-- "your trial ended" — reintroducing the #841 dead end through a side door.
--
-- Net effect for a user-client INSERT: same capability as before (no AI), a
-- truthful reason, and no way to reset the clock.

CREATE OR REPLACE FUNCTION public.protect_user_profiles_entitlements()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $$
BEGIN
  -- Service-role client (Stripe webhook, admin, the API's own profile
  -- creation) may set entitlements freely.
  IF (SELECT auth.role()) = 'service_role' THEN
    RETURN NEW;
  END IF;
  -- Everyone else is a user hitting PostgREST directly. Pin the server-trusted
  -- columns: safe defaults on INSERT, unchanged (OLD) on UPDATE.
  IF TG_OP = 'INSERT' THEN
    NEW.plan := 'trial';
    NEW.llm_enabled := true;
    NEW.llm_monthly_budget_usd := NULL;
    NEW.max_active_targets := NULL;
    NEW.trial_started_at := 'epoch'::timestamptz;  -- already expired; see header
  ELSE
    NEW.plan := OLD.plan;
    NEW.llm_enabled := OLD.llm_enabled;
    NEW.llm_monthly_budget_usd := OLD.llm_monthly_budget_usd;
    NEW.max_active_targets := OLD.max_active_targets;
    NEW.trial_started_at := OLD.trial_started_at;
  END IF;
  RETURN NEW;
END;
$$;
