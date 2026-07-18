-- SEC-H1 (audit 2026-07-18): harden user_profiles against direct PostgREST writes.
--
-- The Supabase anon key ships in the browser, so a logged-in user can PATCH
-- their own user_profiles row straight to PostgREST. RLS row-scopes the table
-- (auth.uid() = user_id) but does NOT column-scope it, and `authenticated`
-- holds table-wide UPDATE. Two live abuses:
--
--   1. Entitlement escalation — set plan='pro' / an unbounded
--      llm_monthly_budget_usd / max_active_targets / llm_enabled=true → grant
--      yourself Pro + host-funded LLM inference, bypassing Stripe and the
--      FastAPI Pydantic whitelist.
--   2. SMS-cost abuse — sms_daily_limit is a user pref bounded 1..50 in Pydantic
--      ONLY (NotificationPreferencesUpdate); a direct PATCH could set 1e6.
--
-- Fix 1 (trigger): make the server-trusted entitlement columns immutable to any
-- non-service_role caller. The billing webhook + admin write via the
-- service-role client (role 'service_role', get_supabase_pool) and are
-- unaffected; the app's own profile-update model never sends these columns.
-- ⚠️ A NEW entitlement column MUST be added to this trigger.
--
-- Fix 2 (CHECK): pin sms_daily_limit to the same 1..50 bound at the DB so it
-- can't be bypassed. Prod verified 0 rows out of range before adding.

CREATE OR REPLACE FUNCTION public.protect_user_profiles_entitlements()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $$
BEGIN
  -- Service-role client (Stripe webhook, admin) may set entitlements freely.
  IF (SELECT auth.role()) = 'service_role' THEN
    RETURN NEW;
  END IF;
  -- Everyone else is a user hitting PostgREST directly. Pin the server-trusted
  -- columns: safe defaults on INSERT, unchanged (OLD) on UPDATE.
  IF TG_OP = 'INSERT' THEN
    NEW.plan := 'free';
    NEW.llm_enabled := true;
    NEW.llm_monthly_budget_usd := NULL;
    NEW.max_active_targets := NULL;
  ELSE
    NEW.plan := OLD.plan;
    NEW.llm_enabled := OLD.llm_enabled;
    NEW.llm_monthly_budget_usd := OLD.llm_monthly_budget_usd;
    NEW.max_active_targets := OLD.max_active_targets;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_user_profiles_entitlements ON public.user_profiles;
CREATE TRIGGER trg_protect_user_profiles_entitlements
  BEFORE INSERT OR UPDATE ON public.user_profiles
  FOR EACH ROW
  EXECUTE FUNCTION public.protect_user_profiles_entitlements();

ALTER TABLE public.user_profiles
  ADD CONSTRAINT user_profiles_sms_daily_limit_range
  CHECK (sms_daily_limit IS NULL OR (sms_daily_limit >= 1 AND sms_daily_limit <= 50));
