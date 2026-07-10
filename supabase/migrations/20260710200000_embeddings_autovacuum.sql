-- Extend the hot-table autovacuum tuning (20260707140000) to the embedding
-- spine. ``job_embeddings`` became one of the churniest tables in the DB
-- during the voyage-3.5 migration (30k+ upserts of 8-16KB vector rows), and
-- ``prescan_shadow`` appends one row per (job, target) admission decision
-- with a 30d retention purge — both accumulate dead tuples far faster than
-- the default 20% scale factor fires, degrading index-only scans to heap
-- fetches (the #261 stale-visibility-map lesson) and compounding disk IO on
-- an instance whose IO budget the 2026-07-10 incident showed is precious.

ALTER TABLE public.job_embeddings SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

ALTER TABLE public.prescan_shadow SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
