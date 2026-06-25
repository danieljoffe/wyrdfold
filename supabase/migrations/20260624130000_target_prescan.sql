-- #60 Pre-scan (embeddings pre-filter), Phase 2: per-target embedding + threshold.
--
-- Phase 1 (20260624120000_job_embeddings.sql) gave each JOB a cached vector.
-- This phase adds the QUERY side: each active target gets a cached embedding of
-- its (label + search_keywords) text, plus a per-target cosine threshold. Phase
-- 3 (#68) will admit a job to the expensive per-target LLM grade iff
-- cosine(job_vec, target_vec) >= target.prescan_cosine_threshold — so cost
-- scales with jobs, not jobs×targets, and the LLM grade stays the real cut.
--
-- All three columns are NULLABLE ⇒ INERT. NULL ``embedding`` = target not yet
-- embedded; NULL ``prescan_cosine_threshold`` = no calibrated gate, so the Phase
-- 3 reader must treat NULL as "don't pre-filter" (admit everything, exactly
-- today's behavior). Nothing reads or writes these until the pre-scan code path
-- is flipped on — merging this migration triggers no embedding spend and changes
-- no runtime behavior.
--
-- Mirrors job_embeddings' vector choice: the same ``extensions.vector(1024)``
-- type (voyage-3 is 1024-dim). NO vector index here — ``targets`` holds only a
-- handful of rows, so a sequential cosine scan over targets is trivial and an
-- HNSW index would be pure overhead (the job side, which is large, carries the
-- index). ``embedding_text_hash`` is the re-embed skip key (sha256 over the
-- embedded text), mirroring job_embeddings.content_hash / jobs.qualified_hash:
-- a target whose label+keywords are unchanged re-derives the same hash and the
-- writer skips the Voyage call.
--
-- targets is service-role-written and read through the targets CRUD / SECURITY
-- DEFINER RPCs; these additive columns need no GRANT/REVOKE/POLICY changes —
-- same posture as 20260624110000_job_qualification_tags.sql (jobs columns) and
-- the rest of the targets-column history.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Re-running the whole file is a no-op.
-- Additive only (no index, no constraint, no destructive DDL) — clears both
-- migration-safety guards (tests/test_migration_safety.py).
--
-- Reversible (manual down — we do not ship a down file):
--   ALTER TABLE public.targets
--     DROP COLUMN IF EXISTS embedding,
--     DROP COLUMN IF EXISTS embedding_text_hash,
--     DROP COLUMN IF EXISTS prescan_cosine_threshold;

-- ---------------------------------------------------------------------------
-- Per-target pre-scan columns. All nullable: a target gets a vector only once
-- the Phase-2 target-embed backfill runs (absence = not-yet-embedded), and a
-- threshold only once calibration writes one (NULL = no gate ⇒ admit all).
-- ---------------------------------------------------------------------------
ALTER TABLE "public"."targets"
    ADD COLUMN IF NOT EXISTS "embedding" "extensions"."vector"(1024),
    ADD COLUMN IF NOT EXISTS "embedding_text_hash" "text",
    ADD COLUMN IF NOT EXISTS "prescan_cosine_threshold" real;

COMMENT ON COLUMN "public"."targets"."embedding" IS 'Pre-scan (#60): the 1024-dim voyage-3 vector of this target''s (label + search_keywords) text, embedded as a QUERY. Written by the Phase-2 target-embed backfill. NULL = not yet embedded.';
COMMENT ON COLUMN "public"."targets"."embedding_text_hash" IS 'Pre-scan (#60): sha256 over the embedded target text (label + keywords). Lets the writer skip re-embedding a target whose text is unchanged.';
COMMENT ON COLUMN "public"."targets"."prescan_cosine_threshold" IS 'Pre-scan (#60): per-target cosine cutoff. Phase 3 admits a job to LLM grading iff cosine(job_vec, target_vec) >= this. Calibrated on clean LLM-graded labels. NULL = no gate (admit all — today''s behavior).';
