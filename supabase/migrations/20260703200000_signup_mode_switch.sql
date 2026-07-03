-- Phase 3 slice 5 — the open-signup MECHANISM (docs/plan-wyrdfold-
-- deployment-modes.md). Built switchable-but-closed: flipping public
-- signup becomes ONE operator DB write (via POST /admin/signup-mode),
-- not a Supabase config push or a redeploy.
--
-- 1. `app_settings` — single-row operational switches. Deny-all RLS
--    (no policies) + explicit revokes: only the service-role backend
--    reads/writes it, and the auth hook reads it as its DEFINER owner.
--    Seeded with signup_mode='closed' — behavior is unchanged until an
--    operator deliberately flips it.
--
-- 2. `hook_restrict_wyrdfold_beta` gains the mode branch: 'open' admits
--    any well-formed signup (the hook stays installed as the future
--    blocklist site); anything else — 'closed', missing row, NULL —
--    falls through to the existing beta-invites allowlist, so the safe
--    posture survives a broken or absent setting.

CREATE TABLE IF NOT EXISTS public.app_settings (
    key text PRIMARY KEY,
    value text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

ALTER TABLE public.app_settings ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.app_settings FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.app_settings TO service_role;

INSERT INTO public.app_settings (key, value)
VALUES ('signup_mode', 'closed')
ON CONFLICT (key) DO NOTHING;

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
