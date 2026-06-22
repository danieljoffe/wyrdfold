-- Record the re-score projection that drove each learn decision (#5 P4).
--
-- Before a high-confidence ProfilePatch auto-applies, the learner projects it
-- over the target's recent scored jobs (deterministic keyword re-score) and
-- stages instead of applying when the projected churn is an outlier — the
-- "learning-rate cap". Persisting the projection makes the apply/stage
-- decision auditable and the thresholds tunable against real data.
--
-- Nullable: empty patches, low-confidence stages, and pre-P4 rows have none.
ALTER TABLE "public"."target_learning_log"
    ADD COLUMN IF NOT EXISTS "projection" "jsonb";
