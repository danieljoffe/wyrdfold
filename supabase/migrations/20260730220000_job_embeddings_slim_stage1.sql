-- job_embeddings slim-down, stage 1 (Disk IO incident 2026-07-30).
--
-- 1. DROP the HNSW index. No serving-path query does ANN over
--    job_embeddings: the prescan gate/ordering fetch vectors BY PK and
--    compute cosine in Python (no `<=>` anywhere in app code or RPCs); the
--    index's 15.2k lifetime scans trace to offline calibration scripts. It
--    was ~473 MB — still sized for the pre-retention 60k rows — and its
--    INSERT-time graph maintenance was the #1 Disk IO consumer (~3.4M
--    shared blocks read / ~34k exec-seconds across ~36k upserts, plus the
--    57014 statement-timeout class). Recreate deliberately if/when the #90
--    cosine gate flips to true ANN admission (consider IVFFlat then).
--
-- 2. Store vectors as halfvec(1024) (pgvector 0.8): float16 halves the
--    payload (4,100 -> 2,052 bytes/vector) with negligible cosine drift for
--    a coarse relevance signal, and the type rewrite compacts the ~340 MB
--    of dead heap left by the retention purge. Text I/O is identical
--    ("[...]"), and the API read path (ast.literal_eval -> floats) is
--    representation-agnostic — RUNNING prod code is unaffected, so this is
--    safe to apply ahead of the release (backward-compatible by
--    construction). Existing 1024-d values cast losslessly-enough in place;
--    no re-embed needed and per-target cosine calibration survives.
--
-- Stage 2 rides the lazy-embedding release: on-demand materialization +
-- halfvec(512) via voyage-3.5 output_dimension (re-embeds the small
-- read-set only, plus target vectors; recalibrates thresholds if/when #90
-- enforces).

DROP INDEX IF EXISTS "public"."idx_job_embeddings_embedding";

ALTER TABLE "public"."job_embeddings"
  ALTER COLUMN "embedding" TYPE extensions.halfvec(1024)
  USING "embedding"::extensions.halfvec(1024);
