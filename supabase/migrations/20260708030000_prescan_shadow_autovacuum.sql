-- Autovacuum audit follow-on (post-#192): proactive tuning for prescan_shadow.
--
-- The stale-visibility-map fix (20260707140000) tuned scores/jobs/llm_costs —
-- the tables read via index-only scans / aggregations on LIVE hot paths
-- (/insights, /profile/llm-usage), where a bulk write left the VM stale and
-- degraded those scans into tens of thousands of heap fetches.
--
-- An audit of every remaining hot-write table found the LIVE latency risk fully
-- covered by that migration: the other large table read on hot paths is
-- job_embeddings, whose vector reads always hit the heap (VM-independent), and
-- the small/tiny tables (sources 1.7k rows, user_profiles, targets, …) are far
-- too small for a stale VM to matter regardless of dead-tuple %.
--
-- The one large bulk-write table still on DEFAULT autovacuum is prescan_shadow:
-- the pre-scan disagreement log, INSERT-heavy (~44k rows/day) with a periodic
-- 30-day retention purge, growing toward ~1.3M rows. It has no live-endpoint
-- reader, so it's not a latency risk — but on the default autovacuum
-- (scale_factor 0.20) a retention purge would leave ~20% of the table as dead
-- bloat before cleanup fired, and insert-heavy growth wouldn't be frozen /
-- marked all-visible until +20%. Tune it proactively before it scales:
--   * vacuum  0.05 — clean post-purge dead rows sooner (less bloat / disk);
--   * analyze 0.02 — keep planner stats fresh as it grows;
--   * insert  0.10 — vacuum insert-heavy growth sooner (freeze + visibility map).
-- Reversible, table-local; no data change.
ALTER TABLE public.prescan_shadow SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02,
    autovacuum_vacuum_insert_scale_factor = 0.10
);
