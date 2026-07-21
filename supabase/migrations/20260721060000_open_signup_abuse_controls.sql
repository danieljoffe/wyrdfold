-- Open-signup abuse control: a disposable-email blocklist enforced by the
-- before-user-created hook when signup_mode='open'.
--
-- Context: the magic-link SEND is browser -> GoTrue direct, so send-rate /
-- email-bombing limits live in GoTrue's own rate-limit config (dashboard:
-- Auth -> Rate Limits — `email_sent`, `sign_in_sign_ups`), NOT in app code.
-- The one app-code chokepoint on OPEN signup is this hook — its original
-- comment already earmarked it as "the future blocklist site". This adds the
-- first blocklist: reject signups from known disposable/throwaway email
-- providers, which is where casual open-signup abuse (junk accounts) starts.
--
-- Note the free tier is BYOK (the user's own key pays inference), so a fake
-- account can't drain the owner's LLM budget — this is hygiene / defense in
-- depth, not the primary control. Only the `mode='open'` branch is touched;
-- the closed-beta invite gate and the null-email guard are byte-for-byte the
-- originals (see 20260703200000_signup_mode_switch.sql).

CREATE TABLE IF NOT EXISTS public.blocked_email_domains (
    domain text PRIMARY KEY,
    reason text NOT NULL DEFAULT 'disposable',
    added_at timestamp with time zone NOT NULL DEFAULT now()
);

-- Deny-all RLS + explicit revokes, mirroring app_settings: only the
-- service-role backend manages it, and the hook reads it as its DEFINER owner
-- (postgres). Extensible — add rows to block more domains; the hook reads live.
ALTER TABLE public.blocked_email_domains ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.blocked_email_domains FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.blocked_email_domains TO service_role;

-- Starter set of common disposable/throwaway providers (lowercase, bare
-- domain). Not exhaustive by design — it catches the lazy majority; extend the
-- table as new services appear. All entries lowercased to match the hook's
-- `lower(email)` domain extraction.
INSERT INTO public.blocked_email_domains (domain) VALUES
    ('mailinator.com'), ('guerrillamail.com'), ('guerrillamail.info'),
    ('guerrillamail.net'), ('sharklasers.com'), ('10minutemail.com'),
    ('10minutemail.net'), ('temp-mail.org'), ('tempmail.com'), ('tempmailo.com'),
    ('throwawaymail.com'), ('yopmail.com'), ('yopmail.fr'), ('getnada.com'),
    ('nada.email'), ('maildrop.cc'), ('mailnesia.com'), ('trashmail.com'),
    ('trashmail.de'), ('dispostable.com'), ('fakeinbox.com'), ('mytemp.email'),
    ('mohmal.com'), ('emailondeck.com'), ('mintemail.com'), ('mailcatch.com'),
    ('spam4.me'), ('tempinbox.com'), ('tempr.email'), ('discard.email'),
    ('moakt.com'), ('tmpmail.org'), ('luxusmail.org'), ('1secmail.com'),
    ('1secmail.net'), ('1secmail.org'), ('cs.email'), ('minuteinbox.com'),
    ('inboxkitten.com'), ('mail-temp.com')
ON CONFLICT (domain) DO NOTHING;

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
    -- Open-signup abuse control (this migration): reject disposable/throwaway
    -- email providers. Fires only on the OPEN path — closed-beta invites are
    -- a pre-vetted allowlist, so the domain check would be redundant there.
    IF EXISTS (
      SELECT 1 FROM public.blocked_email_domains
      WHERE domain = split_part(user_email, '@', 2)
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
