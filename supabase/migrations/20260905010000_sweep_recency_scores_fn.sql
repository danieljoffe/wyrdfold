-- #604: set-based recency sweep.
--
-- The nightly recency refresh was the DB's #1 statement by total time over
-- the 27-day pg_stat_statements window: an OFFSET-paged walk of every live
-- scores row (9,158 calls @ 254ms mean = 2,322s) feeding ~200k row-updates
-- per night through per-chunk bulk_update_recency_scores calls (36,959
-- calls, 857s — the #2 statement). Besides the direct poller-window
-- contention, the nightly rewrite of essentially every live row scatters
-- and bloats the scores heap (379 MB), which is what pushes the /jobs
-- candidate-window planner off the scores-driven plan a fresh replica
-- chooses.
--
-- This function replaces the whole walk: one keyset-paged, set-based
-- UPDATE per batch, computed entirely in SQL, that only touches rows whose
-- stored value actually changes (grace-window and floored rows stop being
-- rewritten every night). The caller loops it to exhaustion — ~14 short
-- transactions instead of ~340 reads + ~37,000 update calls.
--
-- Semantics mirror app/services/recency.py exactly (the decay constants are
-- passed IN from the module, which stays the single source of truth):
--   age_days   = max(0, now - COALESCE(jobs.source_posted_at, cataloged_at))
--                in days; a job with neither date reads as fresh (age 0).
--   multiplier = max(p_floor, 1 - max(0, age_days - p_grace_days) * p_daily_decay)
--   new value  = round(score * multiplier), or score verbatim when
--                p_enabled is false (the flag-off identity).
-- Scope mirrors the Python sweep: excluded = false, job not archived
-- (a score for an archived job is never listed; its stored value stays
-- frozen, as before). Arithmetic runs in double precision to match
-- Python's floats; the final round() is numeric (half-away-from-zero) —
-- parity across the practical input grid is pinned by
-- tests/integration/test_recency_sweep_parity.py.
--
-- Cursor contract: pass p_after_id = the previous batch's last_id
-- (all-zeros uuid to start). The LIMIT applies AFTER the join, so
-- "scanned < p_batch_size" means the id range is exhausted — that, or a
-- NULL last_id, ends the loop.

CREATE OR REPLACE FUNCTION public.sweep_recency_scores(
    p_enabled boolean,
    p_after_id uuid,
    p_batch_size integer,
    p_grace_days double precision,
    p_daily_decay double precision,
    p_floor double precision
) RETURNS TABLE (scanned integer, written integer, last_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = ''
AS $$
WITH batch AS (
    SELECT s.id,
           COALESCE(s.score, 0) AS score,
           COALESCE(j.source_posted_at, j.cataloged_at) AS seen
    FROM public.scores s
    JOIN public.jobs j ON j.id = s.job_posting_id
    WHERE s.excluded = false
      AND j.archived_at IS NULL
      AND s.id > p_after_id
    ORDER BY s.id
    LIMIT p_batch_size
),
computed AS (
    SELECT b.id,
           CASE WHEN p_enabled THEN
               round((b.score::double precision * GREATEST(
                   p_floor,
                   1.0 - GREATEST(
                       0.0,
                       GREATEST(
                           0.0,
                           COALESCE(EXTRACT(EPOCH FROM (now() - b.seen)), 0.0)
                       ) / 86400.0 - p_grace_days
                   ) * p_daily_decay
               ))::numeric)::integer
           ELSE b.score
           END AS new_score
    FROM batch b
),
updated AS (
    UPDATE public.scores s
       SET recency_score = c.new_score
      FROM computed c
     WHERE s.id = c.id
       AND s.recency_score IS DISTINCT FROM c.new_score
     RETURNING s.id
)
SELECT (SELECT count(*)::integer FROM batch),
       (SELECT count(*)::integer FROM updated),
       (SELECT b.id FROM batch b ORDER BY b.id DESC LIMIT 1);
$$;

-- Service-role only, like bulk_update_recency_scores: it runs as postgres
-- (SECURITY DEFINER, bypasses RLS) and rewrites the shared catalog's sort
-- key. Postgres grants EXECUTE to PUBLIC on every new function by default
-- (see 20260621120000) — revoke it explicitly.
REVOKE ALL ON FUNCTION public.sweep_recency_scores(
    boolean, uuid, integer, double precision, double precision, double precision
) FROM PUBLIC, anon, authenticated;
