-- #604: partial ordered indexes for the two-query candidate windows.
--
-- The /jobs two-query fallback draws each tier's 1,000-row window ORDER BY a
-- scores column, but no index matched the window's order, so the planner
-- derived liveness through the jobs join and walked every live job probing
-- scores' unique key (measured on prod: 2.8s / ~89k buffers for the Pending
-- window, 1.1s to return 96 graded rows). These two indexes mirror each
-- tier's exact ORDER BY over the denormalized ``job_is_live`` predicate the
-- query now filters on, turning the window into a LIMIT-driven ordered index
-- scan that reads only the head it returns — bounded cost regardless of
-- per-target volume or heap bloat.
--
-- NULLS LAST is load-bearing: the query orders ``DESC NULLS LAST`` (a NULL
-- would otherwise crowd the head of the window), and a plain DESC index is
-- NULLS FIRST — it cannot serve that order and the sort node comes back
-- (verified during the #604 measurement pass).
--
-- Footprint: partial on (job_is_live AND NOT excluded [AND is_graded arm]),
-- single-digit MB at today's 271k live rows.
--
-- index-lock-ok: prod gets the ONLINE treatment at release — both indexes
-- built by hand as CREATE INDEX CONCURRENTLY one-shots (scores is
-- continuously poller-written; same protocol as 20260720030000), then this
-- file is stamped. The plain statements below are for local/CI/fresh
-- schemas, where scores is empty and the build is instant. IF NOT EXISTS
-- makes the stamped file a no-op wherever the online build already ran.

CREATE INDEX IF NOT EXISTS idx_scores_pending_window
    ON public.scores (target_id, job_first_seen_at DESC NULLS LAST, job_posting_id DESC)
    WHERE (job_is_live AND NOT excluded AND NOT is_graded);

CREATE INDEX IF NOT EXISTS idx_scores_graded_window
    ON public.scores (target_id, recency_score DESC NULLS LAST, job_posting_id DESC)
    WHERE (job_is_live AND NOT excluded AND is_graded);
