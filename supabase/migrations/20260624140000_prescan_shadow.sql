-- #60 Pre-scan (embeddings pre-filter), Phase 3 SHADOW MODE: disagreement matrix.
--
-- Append-only shadow observations for the pre-scan disagreement matrix (#68).
-- Analysis-only / TEMPORARY: this table exists to compare the would-be cosine
-- gate decision against the live keyword admit decision on real poll cycles,
-- WITHOUT changing what gets graded. The keyword gate still drives admission;
-- Phase 3 records both decisions side-by-side so we can size the disagreement
-- (does cosine admit genuine matches and drop the SDRs?) BEFORE any flip. The
-- gate FLIP — making cosine drive admission — is a LATER phase informed by this
-- data and is deliberately NOT built here.
--
-- Phase 1 (20260624120000_job_embeddings.sql) gave each JOB a cached vector;
-- Phase 2 (20260624130000_target_prescan.sql) gave each TARGET a vector + a
-- per-target ``prescan_cosine_threshold``. This phase only OBSERVES: for each
-- (job, target) the poller already admitted-or-not via keywords, it also
-- computes cosine(job_vec, target_vec) and writes one row here. All cosine
-- columns are NULLABLE — when a job/target vector or the threshold is missing
-- (the common case until the Phase 1/2 backfills run) the cosine side is NULL
-- and only the keyword side is recorded.
--
-- INERT by default: nothing writes this table unless an operator flips
-- PRESCAN_SHADOW_ENABLED (app/config.py, default off) AND the Phase 1/2 vectors
-- have been populated. Merging this migration changes no runtime behavior and
-- triggers no embedding spend (the writer is the poller's flag-gated, best-effort
-- shadow hook; flag off ⇒ no rows, no cosine computation).
--
-- RLS + grants posture mirrors job_embeddings: GLOBAL, service-role-written
-- internal data with NO per-user owner column, so ENABLE ROW LEVEL SECURITY with
-- NO policy — anon / authenticated are denied entirely and only ``service_role``
-- (the poller) bypasses RLS. The same GRANT triple as job_embeddings.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS and DROP POLICY IF EXISTS is
-- unnecessary (no policy). Re-running the whole file is a no-op. Additive only
-- (a new table + its index, no destructive DDL, build runs over an empty table)
-- — clears both migration-safety guards (tests/test_migration_safety.py).
--
-- Reversible (manual down — we do not ship a down file; this table is meant to be
-- dropped once the disagreement matrix has been analysed and the flip decided):
--   DROP TABLE IF EXISTS public.prescan_shadow;  (cascades its index)

-- ---------------------------------------------------------------------------
-- 1. Observation table. One row per (job, target) the poller scored while the
--    shadow flag was on. ``keyword_*`` is the live decision that actually drove
--    admission; ``cosine`` / ``cosine_admit`` / ``threshold`` are the would-be
--    cosine-gate decision (all NULL when the inputs aren't populated yet —
--    fail-soft, see app/services/embeddings/prescan_gate.py). ``id`` PK so rows
--    are individually addressable; no uniqueness on (job, target) — this is an
--    append-only log, a job re-polled across cycles yields multiple rows and the
--    ``observed_at`` timestamp distinguishes them.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "public"."prescan_shadow" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "job_posting_id" "uuid" NOT NULL,
    "target_id" "uuid" NOT NULL,
    "observed_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "keyword_admit" boolean,
    "keyword_score" integer,
    "cosine" real,
    "cosine_admit" boolean,
    "threshold" real,
    CONSTRAINT "prescan_shadow_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "prescan_shadow_job_posting_id_fkey" FOREIGN KEY ("job_posting_id")
        REFERENCES "public"."jobs"("id") ON DELETE CASCADE,
    CONSTRAINT "prescan_shadow_target_id_fkey" FOREIGN KEY ("target_id")
        REFERENCES "public"."targets"("id") ON DELETE CASCADE
);

ALTER TABLE "public"."prescan_shadow" OWNER TO "postgres";

COMMENT ON TABLE "public"."prescan_shadow" IS 'Pre-scan (#60/#68) Phase 3 SHADOW: append-only disagreement matrix — the would-be cosine gate decision logged alongside the live keyword admit decision, without changing grading. Analysis-only/temporary; written by the poller behind PRESCAN_SHADOW_ENABLED. service-role-only (RLS denies anon/authenticated).';
COMMENT ON COLUMN "public"."prescan_shadow"."keyword_admit" IS 'Pre-scan shadow (#68): the LIVE keyword/Phase-1 admit decision that actually drove this job to (or away from) grading. This is the decision in force; cosine is observed-only.';
COMMENT ON COLUMN "public"."prescan_shadow"."keyword_score" IS 'Pre-scan shadow (#68): the Stage-2 keyword score for this (job, target) at observation time.';
COMMENT ON COLUMN "public"."prescan_shadow"."cosine" IS 'Pre-scan shadow (#68): cosine(job_vec, target_vec). NULL when the job or target vector is not yet populated (Phase 1/2 backfills not run).';
COMMENT ON COLUMN "public"."prescan_shadow"."cosine_admit" IS 'Pre-scan shadow (#68): the would-be cosine gate verdict (cosine >= threshold). NULL when cosine or threshold is unavailable. OBSERVED ONLY — does not drive admission in this phase.';
COMMENT ON COLUMN "public"."prescan_shadow"."threshold" IS 'Pre-scan shadow (#68): the target''s prescan_cosine_threshold at observation time. NULL = no calibrated gate.';

-- ---------------------------------------------------------------------------
-- 2. Index for the analysis queries — the disagreement matrix is sliced
--    per-target over time, so (target_id, observed_at). prescan_shadow is an
--    append-only log written in the poll path, but the per-poll write volume is
--    bounded (one row per scored job×target) and this is a plain btree, not a
--    hot continuously-rebuilt structure, so a plain CREATE INDEX is fine — no
--    ``index-lock-ok`` marker needed (the build also runs over an empty table at
--    apply time; see tests/test_migration_safety.py).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS "idx_prescan_shadow_target_observed"
    ON "public"."prescan_shadow" ("target_id", "observed_at");

-- ---------------------------------------------------------------------------
-- 3. RLS + grants. GRANT triple mirrors job_embeddings; RLS is enabled with NO
--    policy, so anon / authenticated are denied and only service_role (which
--    bypasses RLS) can read/write. See the header for why this table has no
--    per-user ownership policy.
-- ---------------------------------------------------------------------------
ALTER TABLE "public"."prescan_shadow" ENABLE ROW LEVEL SECURITY;

GRANT ALL ON TABLE "public"."prescan_shadow" TO "anon";
GRANT ALL ON TABLE "public"."prescan_shadow" TO "authenticated";
GRANT ALL ON TABLE "public"."prescan_shadow" TO "service_role";
