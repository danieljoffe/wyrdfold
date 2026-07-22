-- Hardening review 2026-07-21 (DB-F4): match disposable-email domains on
-- subdomains too, not just the exact registrable domain.
--
-- 20260721060000's blocklist check was `domain = split_part(email,'@',2)`, an
-- exact match. Several blocked providers (Mailinator, Guerrilla-class, 1secmail)
-- hand out addresses on arbitrary subdomains — user@x.mailinator.com is not
-- equal to mailinator.com and sailed straight through the open-signup path.
-- Subdomain routing IS the common case for these services, so the exact-match
-- check missed the majority it was meant to catch.
--
-- Fix: also match `<anything>.<blocked-domain>` via LIKE. blocked_email_domains
-- is service-role-managed (deny-all RLS) and holds only literal domains with no
-- LIKE metacharacters, so `'%.' || domain` is safe. The table is ~40 rows; the
-- extra predicate is a trivial seq scan.
--
-- Everything else in the hook is byte-for-byte the original (20260721060000):
-- only the EXISTS predicate changes.

CREATE OR REPLACE FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
DECLARE
  user_email text;
  mode text;
BEGIN
  user_email := lower(event->'user'->>'email');

  IF user_email IS NULL THEN
    RETURN jsonb_build_object(
      'error', jsonb_build_object(
        'message', 'Email is required to sign up.',
        'http_code', 400
      )
    );
  END IF;

  -- Phase 3 slice 5: the operator switch. Only an explicit 'open' admits
  -- publicly; a missing/NULL/unknown value stays closed (fail-safe).
  SELECT value INTO mode FROM public.app_settings WHERE key = 'signup_mode';
  IF mode = 'open' THEN
    -- Open-signup abuse control: reject disposable/throwaway email providers,
    -- matching the registrable domain OR any subdomain of it (DB-F4). Fires
    -- only on the OPEN path — closed-beta invites are a pre-vetted allowlist,
    -- so the domain check would be redundant there.
    IF EXISTS (
      SELECT 1 FROM public.blocked_email_domains
      WHERE split_part(user_email, '@', 2) = domain
         OR split_part(user_email, '@', 2) LIKE '%.' || domain
    ) THEN
      RETURN jsonb_build_object(
        'error', jsonb_build_object(
          'message', 'Please sign up with a permanent email address.',
          'http_code', 400
        )
      );
    END IF;
    RETURN '{}'::jsonb;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM public.wyrdfold_beta_invites
    WHERE lower(email) = user_email
  ) THEN
    -- Match GoTrue's standard "user not found" error verbatim.
    RETURN jsonb_build_object(
      'error', jsonb_build_object(
        'message', 'User not found',
        'http_code', 400
      )
    );
  END IF;

  RETURN '{}'::jsonb;
END;
$$;

ALTER FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") OWNER TO "postgres";
