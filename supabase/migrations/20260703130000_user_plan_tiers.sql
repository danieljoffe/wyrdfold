-- Phase 3 slice 2 — the tier model (docs/plan-wyrdfold-deployment-modes.md,
-- Phase 3; pricing/shape locked 2026-07-03).
--
-- 1. `user_profiles.plan` — 'free' (BYOK: the user's own OpenRouter key
--    pays inference) | 'starter' | 'pro' (managed: host keys + a per-tier
--    interactive-dollar quota). The plan binds ONLY in saas deployment
--    mode; self-host ignores it (instance env key = the owner's BYOK).
--
--    Backfill order matters (Copilot on #205): the column is added
--    WITHOUT a default, so at backfill time `plan IS NULL` identifies
--    exactly the rows that pre-date the tier model — the invited beta
--    cohort, comped to 'pro' until open signup. Only then does the
--    'free' default (for future signups) + NOT NULL + CHECK land. This
--    can never comp a legitimately-'free' account, even if the
--    statements are replayed against a DB where free users already
--    exist.
--
-- 2. `total_billable_spend_since` — the managed-tier quota counts
--    INTERACTIVE spend only (analysis, tailor, derive, …): the ledger
--    attributes catalog/background work (triage, fit-grading, polling)
--    to the triggering user, and counting it would drain a user's quota
--    while they sleep. Background cost is bounded structurally by the
--    per-tier active-target cap instead. Same shape/guard/grants as
--    total_spend_since (20260620130000): SQL STABLE, pinned search_path,
--    a JWT caller may only query their own spend, service-role exempt;
--    EXECUTE for authenticated + service_role only (PUBLIC/anon revoked —
--    the #16 lesson).

ALTER TABLE public.user_profiles
    ADD COLUMN IF NOT EXISTS plan text;

-- Beta-cohort comp (see header): rows from before the tier model.
UPDATE public.user_profiles SET plan = 'pro' WHERE plan IS NULL;

ALTER TABLE public.user_profiles
    ALTER COLUMN plan SET DEFAULT 'free';
ALTER TABLE public.user_profiles
    ALTER COLUMN plan SET NOT NULL;
ALTER TABLE public.user_profiles
    DROP CONSTRAINT IF EXISTS user_profiles_plan_check;
ALTER TABLE public.user_profiles
    ADD CONSTRAINT user_profiles_plan_check
    CHECK (plan IN ('free', 'starter', 'pro'));

CREATE OR REPLACE FUNCTION public.total_billable_spend_since(
    p_user_id uuid,
    p_since timestamp with time zone,
    p_excluded_purposes text[]
) RETURNS numeric
    LANGUAGE sql STABLE
    SET search_path TO 'public', 'pg_catalog'
AS $$
  SELECT COALESCE(SUM(cost_usd), 0)::numeric
  FROM   public.llm_costs
  WHERE  (
           (p_user_id IS NULL AND user_id IS NULL)
           OR user_id = p_user_id
         )
    AND  (p_since IS NULL OR created_at >= p_since)
    AND  (
           p_excluded_purposes IS NULL
           OR purpose <> ALL (p_excluded_purposes)
         )
    -- guard: a JWT caller may only query their own spend; service-role exempt.
    AND  (auth.uid() IS NULL OR p_user_id = auth.uid());
$$;

ALTER FUNCTION public.total_billable_spend_since(uuid, timestamp with time zone, text[])
    OWNER TO postgres;
REVOKE ALL ON FUNCTION public.total_billable_spend_since(uuid, timestamp with time zone, text[])
    FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.total_billable_spend_since(uuid, timestamp with time zone, text[])
    TO authenticated, service_role;
