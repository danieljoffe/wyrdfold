-- #260 (perf): keep the insights RPCs fast after bulk re-scoring.
--
-- insights_targets_groupby / insights_pipeline_status_counts do index-only scans
-- over scores (× jobs, × user_jobs). After a bulk write — e.g. a profile change
-- re-scoring a user's tens-of-thousands of `scores`, or a tagging/archiving pass
-- over `jobs` — the DEFAULT autovacuum (autovacuum_vacuum_scale_factor = 0.20)
-- doesn't fire until ~20% of the whole table is dead, so the visibility map goes
-- stale and the index-only scans fall back to tens of thousands of heap fetches.
-- Measured on prod: targets RPC 6.25s / pipeline RPC 3.49s with a stale VM, vs
-- 0.44s / 0.05s immediately after a manual VACUUM ANALYZE.
--
-- Lower the per-table autovacuum + autoanalyze thresholds on the two hottest
-- write tables so the VM + planner stats stay fresh and the scans stay
-- index-only. Reversible, table-local; no data change.
ALTER TABLE public.scores SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

ALTER TABLE public.jobs SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
