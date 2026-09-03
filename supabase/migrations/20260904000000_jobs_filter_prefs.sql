-- #866: /jobs filter memory moves server-side.
--
-- The FE persisted per-target filter snapshots in localStorage under a
-- GLOBAL key, so on a shared browser a new account inherited the previous
-- user's filters and opened /jobs on "No jobs found" despite having
-- matches. The page deliberately exposes no user id to key client storage
-- by, so the durable fix (owner's call, 2026-09-03) is a per-user server
-- blob: one jsonb map of {target-id-or-__all__: JobsFilterState} on the
-- caller's own user_profiles row.
--
-- Opaque UI state by design: the server never queries INTO it (no index),
-- the API caps its size, and the client re-validates every entry on read
-- (coerceStoredFilters), so schema drift inside the blob is the client's
-- problem to coerce, not a migration's.
--
-- Additive only; safe to apply before the code that uses it ships. Reads
-- and writes ride user_profiles' existing owner RLS policies via the
-- user-scoped client — no new grants.

ALTER TABLE public.user_profiles
    ADD COLUMN IF NOT EXISTS jobs_filter_prefs jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.user_profiles.jobs_filter_prefs IS
    '#866: per-target /jobs filter snapshots ({target_id|__all__: JobsFilterState}). Opaque client-owned UI state; size-capped at the API.';
