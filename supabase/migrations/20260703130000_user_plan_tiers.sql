-- Phase 3 slice 2 — the tier model (docs/plan-wyrdfold-deployment-modes.md,
-- Phase 3; pricing/shape locked 2026-07-03).
--
-- 1. `user_profiles.plan` — 'free' (BYOK: the user's own OpenRouter key
--    pays inference) | 'starter' | 'pro' (managed: host keys + a per-tier
--    interactive-dollar quota). The plan binds ONLY in saas deployment
--    mode; self-host ignores it (instance env key = the owner's BYOK).
--    Existing rows are comped to 'pro' — current users are the invited
--    beta cohort, grandfathered until open signup. New rows default 'free'.
--
-- 2. `total_billable_spend_since` — the managed-tier quota counts
--    INTERACTIVE spend only (analysis, tailor, derive, …): the ledger
--    attributes catalog/background work (triage, fit-grading, polling) to
--    the triggering user, and counting it would drain a user's quota
--    while they sleep. Background cost is bounded structurally by the
--    per-tier active-target cap instead. Same shape/guard/grants as
--    total_spend_since (20260620130000): SQL STABLE, pinned search_path,
--    a JWT caller may only query their own spend, service-role exempt;
--    EXECUTE for authenticated + service_role only (PUBLIC/anon revoked —
--    the #16 lesson).

ALTER TABLE public.user_profiles
    ADD COLUMN IF NOT EXISTS plan text NOT NULL DEFAULT 'free'
        CONSTRAINT user_profiles_plan_check
        CHECK (plan IN ('free', 'starter', 'pro'));

-- Beta-cohort comp (see header). Idempotent: only rows still on the
-- fresh-column default are lifted, so a re-run can't downgrade anyone.
UPDATE public.user_profiles SET plan = 'pro' WHERE plan = 'free';

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
