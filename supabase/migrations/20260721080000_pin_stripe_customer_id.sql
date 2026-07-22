-- Hardening review 2026-07-21 (DB-F2, DB-F8): finish the user_profiles
-- entitlement pin and stop the trigger silently no-oping direct-SQL writes.
--
-- DB-F2 — stripe_customer_id is server-trusted but was left client-writable.
-- 20260718000000 made plan / llm_enabled / llm_monthly_budget_usd /
-- max_active_targets immutable to non-service_role callers, but did NOT pin
-- stripe_customer_id — even though 20260703150000 declares it "only ever
-- written by the service-role backend" and the API trusts it both ways:
-- the Stripe webhook resolves a customer to a user by it
-- (billing.py resolve-by-stripe_customer_id) and POST /billing/portal-session
-- opens Stripe's billing portal on the caller's stored value. Because
-- authenticated holds table-wide UPDATE + a FOR ALL own-row RLS policy and the
-- anon key ships in the browser, a user can PATCH their own row's
-- stripe_customer_id directly. The partial UNIQUE (20260703150000) blocks
-- duplicating a *currently-stored* id, but account erasure deletes the
-- user_profiles row without deleting the Stripe customer, freeing the id — so a
-- churned `cus_...` (leaked via an invoice/support thread) could be claimed and
-- its billing portal opened. The migration's own warning — "⚠️ A NEW
-- entitlement column MUST be added to this trigger" — applied retroactively.
--
-- DB-F8 — the trigger's only exemption was `auth.role() = 'service_role'`, which
-- is NULL on a direct postgres connection (psql, a seed/backfill migration).
-- So an operator `UPDATE user_profiles SET plan=...` silently no-ops with no
-- signal. PostgREST callers always carry request.jwt.claims; a direct DB
-- connection never does — so also exempt the no-JWT context. That is the
-- trusted operator/superuser path (it is not reachable via the anon key), so
-- exempting it is safe and restores expected operator behavior.

CREATE OR REPLACE FUNCTION public.protect_user_profiles_entitlements()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $$
BEGIN
  -- Service-role client (Stripe webhook, admin) may set entitlements freely.
  -- A direct DB connection (operator psql / migration) carries no JWT claims;
  -- it is the trusted superuser path, never the browser anon key — exempt it
  -- too so operator writes aren't silently pinned away (DB-F8).
  IF (SELECT auth.role()) = 'service_role'
     OR current_setting('request.jwt.claims', true) IS NULL THEN
    RETURN NEW;
  END IF;
  -- Everyone else is a user hitting PostgREST directly. Pin the server-trusted
  -- columns: safe defaults on INSERT, unchanged (OLD) on UPDATE.
  IF TG_OP = 'INSERT' THEN
    NEW.plan := 'free';
    NEW.llm_enabled := true;
    NEW.llm_monthly_budget_usd := NULL;
    NEW.max_active_targets := NULL;
    NEW.stripe_customer_id := NULL;  -- DB-F2: set only by the service-role backend
  ELSE
    NEW.plan := OLD.plan;
    NEW.llm_enabled := OLD.llm_enabled;
    NEW.llm_monthly_budget_usd := OLD.llm_monthly_budget_usd;
    NEW.max_active_targets := OLD.max_active_targets;
    NEW.stripe_customer_id := OLD.stripe_customer_id;  -- DB-F2
  END IF;
  RETURN NEW;
END;
$$;

-- Trigger binding is unchanged (BEFORE INSERT OR UPDATE, via CREATE OR REPLACE).
