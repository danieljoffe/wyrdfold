-- #958: product-quality observability — catalog health per poll cycle.
--
-- The #952 lesson: infrastructure metrics said "3,957 listings ingested"
-- while almost none were relevant to any active target, and nothing watched
-- the funnel at the product level. This migration adds the storage half:
--
--   * catalog_health_cycles — one row per recorded cycle: window intake
--     metrics, corpus-wide quality percentages, the admitted-title token
--     histogram, and whether the distribution tripwire fired. Written by the
--     poller (service role), read by the operator via /admin/catalog-health.
--     Deny-all RLS + explicit revokes, mirroring blocked_email_domains: this
--     is operator telemetry, no user-facing surface reads it.
--
--   * catalog_health_snapshot() — the corpus-wide aggregates (ungraded %,
--     location-unknown %, role_family histogram) in ONE server-side pass
--     over the live corpus, instead of N PostgREST count round-trips per
--     cycle (prod DB IO discipline: one-shot analytics). "Live" mirrors the
--     search corpus gate exactly: archived_at IS NULL AND purged_at IS NULL
--     AND is_us IS NOT FALSE. LANGUAGE sql STABLE, fixed search_path,
--     service_role-only (the poller's client) — unlike the insights RPCs
--     this has no user-facing caller, so anon/authenticated get nothing.
--
-- Additive only; safe to apply before the code that uses it ships.

CREATE TABLE IF NOT EXISTS public.catalog_health_cycles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    computed_at timestamptz NOT NULL DEFAULT now(),
    -- The trailing intake window this row measured (window end = computed_at).
    window_started_at timestamptz NOT NULL,
    -- Intake within the window: everything admitted, and the subset holding
    -- at least one scores row (i.e. relevant to some target's pipeline).
    new_jobs integer NOT NULL DEFAULT 0,
    relevant_jobs integer NOT NULL DEFAULT 0,
    -- Corpus-wide quality (from catalog_health_snapshot at computed_at).
    live_total integer,
    pct_ungraded numeric(5, 2),
    pct_location_unknown numeric(5, 2),
    family_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Window medians / distributions.
    median_admission_age_hours numeric(8, 1),
    top_title_tokens jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- Tripwire: total-variation distance of the window's token distribution
    -- vs the trailing baseline; NULL distance = not evaluated (reason says why).
    tripwire_fired boolean NOT NULL DEFAULT false,
    tripwire_distance numeric(4, 3),
    tripwire_reason text
);

-- Every read path is "most recent first" (admin listing, baseline window,
-- throttle check), so one descending index covers them all.
CREATE INDEX IF NOT EXISTS idx_catalog_health_cycles_computed_at
    ON public.catalog_health_cycles (computed_at DESC);

ALTER TABLE public.catalog_health_cycles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.catalog_health_cycles FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.catalog_health_cycles TO service_role;

CREATE OR REPLACE FUNCTION public.catalog_health_snapshot()
RETURNS jsonb
LANGUAGE sql
STABLE
SET "search_path" TO 'public', 'pg_catalog'
AS $$
    WITH live AS (
        SELECT qualified_at, city, state, country, role_family
        FROM public.jobs
        WHERE archived_at IS NULL
          AND purged_at IS NULL
          AND is_us IS NOT FALSE
    ),
    totals AS (
        SELECT
            count(*)::int AS live_total,
            count(*) FILTER (WHERE qualified_at IS NULL)::int AS ungraded,
            count(*) FILTER (
                WHERE city IS NULL AND state IS NULL AND country IS NULL
            )::int AS location_unknown
        FROM live
    ),
    families AS (
        SELECT COALESCE(
            jsonb_object_agg(fam, n ORDER BY fam), '{}'::jsonb
        ) AS family_counts
        FROM (
            SELECT COALESCE(role_family, 'untagged') AS fam, count(*)::int AS n
            FROM live
            GROUP BY 1
        ) f
    )
    SELECT jsonb_build_object(
        'live_total', t.live_total,
        'ungraded', t.ungraded,
        'location_unknown', t.location_unknown,
        'family_counts', fam.family_counts
    )
    FROM totals t, families fam;
$$;

ALTER FUNCTION public.catalog_health_snapshot() OWNER TO postgres;
REVOKE ALL ON FUNCTION public.catalog_health_snapshot() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.catalog_health_snapshot() TO service_role;
