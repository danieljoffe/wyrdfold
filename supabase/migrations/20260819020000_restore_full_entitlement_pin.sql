-- REGRESSION FIX: 20260819010000 clobbered two earlier hardening fixes.
--
-- WHAT HAPPENED. 20260819010000 (pin trial_started_at) rewrote
-- `protect_user_profiles_entitlements` with `CREATE OR REPLACE FUNCTION`,
-- but it was written against the body from 20260718000000 — NOT the current
-- one from 20260721080000. `CREATE OR REPLACE` replaces the whole function,
-- so it silently reverted both fixes that landed in between:
--
--   DB-F2 — `stripe_customer_id` was pinned because the API trusts it BOTH
--           ways: the Stripe webhook resolves a customer to a user by it, and
--           POST /billing/portal-session opens the billing portal on the
--           caller's stored value. Account erasure deletes the profile row
--           without deleting the Stripe customer, freeing the id — so a
--           churned `cus_...` could be claimed and its portal opened.
--
--   DB-F8 — the no-JWT exemption. Without it a direct DB connection (operator
--           psql, a seed/backfill migration) has `auth.role()` NULL, so an
--           operator `UPDATE user_profiles SET plan=...` silently NO-OPS with
--           no signal.
--
-- Confirmed live on prod before this migration: a user-client PATCH set
-- `stripe_customer_id` to an attacker-chosen value and it stuck.
--
-- HOW IT WAS CAUGHT. The deny-by-default column-classification test (#873),
-- on its first run. That test exists precisely because #872 shipped an
-- unpinned column with everything green — and it immediately found a second
-- one that had been unpinned for hours. A checklist would not have.
--
-- LESSON, recorded in the trigger body below: this function is edited by
-- FULL REPLACEMENT, so every edit must start from the CURRENT definition, not
-- from whichever migration you happen to be reading. The set of pinned columns
-- is now asserted by test_every_server_trusted_column_is_pinned, so a future
-- clobber fails CI instead of shipping.

CREATE OR REPLACE FUNCTION public.protect_user_profiles_entitlements()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $$
BEGIN
  -- ⚠️ EDITING THIS FUNCTION: it is replaced WHOLESALE by CREATE OR REPLACE.
  -- Start from the CURRENT definition (`\sf public.protect_user_profiles_entitlements`),
  -- never from an older migration — 20260819010000 did the latter and dropped
  -- two fixes. Every server-trusted column must appear in BOTH branches, and
  -- in SERVER_TRUSTED in tests/integration/test_entitlement_guard.py (#873).
  --
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
    NEW.plan := 'trial';
    NEW.llm_enabled := true;
    NEW.llm_monthly_budget_usd := NULL;
    NEW.max_active_targets := NULL;
    NEW.stripe_customer_id := NULL;                 -- DB-F2
    NEW.trial_started_at := 'epoch'::timestamptz;   -- #841; already expired
  ELSE
    NEW.plan := OLD.plan;
    NEW.llm_enabled := OLD.llm_enabled;
    NEW.llm_monthly_budget_usd := OLD.llm_monthly_budget_usd;
    NEW.max_active_targets := OLD.max_active_targets;
    NEW.stripe_customer_id := OLD.stripe_customer_id;  -- DB-F2
    NEW.trial_started_at := OLD.trial_started_at;      -- #841
  END IF;
  RETURN NEW;
END;
$$;
