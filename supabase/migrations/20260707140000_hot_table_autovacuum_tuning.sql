-- #260 (perf): keep the insights + llm-usage aggregations fast after bulk writes.
--
-- Several hot read paths scan large per-user row sets in `scores`, `jobs`, and
-- `llm_costs`:
--   * insights_targets_groupby / insights_pipeline_status_counts — index-only
--     scans over scores (× jobs, × user_jobs);
--   * /profile/llm-usage — SUM(cost_usd) over a user's llm_costs window.
-- All three tables are bulk-written (a profile change re-scores ~tens of
-- thousands of `scores`; a tagging/archiving pass rewrites `jobs`; the tagger
-- logs thousands of `llm_costs` rows). The DEFAULT autovacuum
-- (autovacuum_vacuum_scale_factor = 0.20) doesn't fire until ~20% of the WHOLE
-- table is dead, so after a bulk write the visibility map + planner stats go
-- stale and the scans fall back to tens of thousands of heap fetches.
--
-- Measured on prod with a stale VM vs immediately after VACUUM ANALYZE:
--   insights_targets_groupby 6.25s → 0.44s; pipeline_status_counts 3.49s → 0.05s;
--   llm-usage month-spend SUM 1.73s → 0.02s.
--
-- Lower the per-table autovacuum + autoanalyze thresholds on the three hottest
-- write tables so the VM + stats stay fresh. Reversible, table-local; no data
-- change.
ALTER TABLE public.scores SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

ALTER TABLE public.jobs SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);

ALTER TABLE public.llm_costs SET (
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
