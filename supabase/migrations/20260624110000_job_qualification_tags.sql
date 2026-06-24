-- #60 Job qualification firewall: a cheap, cached, target-INDEPENDENT tagger.
--
-- Per-target grading (Phase 1 title triage + Phase 2 fit) re-classifies the
-- same intrinsic facts about a posting (Is it US-based? Is it real? What role
-- family / seniority / employment type?) once per target — N targets pay N
-- times to learn the same thing about one job. This migration adds the storage
-- for a job-level tagger that classifies each posting ONCE and writes the verdict
-- onto the jobs row, so per-target grading can pre-filter cheaply (e.g. "this
-- target only wants US senior+ engineering" → drop non-US / junior / non-eng
-- rows before paying any per-target LLM cost).
--
-- The tagger itself is gated behind QUALIFICATION_ENABLED (default off, see
-- app/config.py) so merging this migration triggers no LLM spend — the columns
-- stay NULL until the flag is flipped per-deploy.
--
-- jobs is service-role-written and read through SECURITY DEFINER RPCs
-- (get_target_jobs, pipeline_counts, ...); it has no per-row RLS or anon/
-- authenticated column grants of its own (the RPCs are the access surface), so
-- these additive columns need no GRANT/REVOKE/POLICY changes — matching
-- 20260614160000_c3_global_archive.sql (jobs.archived_at) and the rest of the
-- jobs-column history.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS, and each
-- ADD CONSTRAINT is wrapped in a ``DO`` block guarded on pg_constraint (Postgres
-- has no ADD CONSTRAINT IF NOT EXISTS). Re-running the whole file is a no-op.
--
-- Reversible (manual down — we do not ship a down file):
--   DROP INDEX IF EXISTS public.idx_jobs_qualification_prefilter;
--   ALTER TABLE public.jobs
--     DROP COLUMN IF EXISTS is_us,
--     DROP COLUMN IF EXISTS us_confidence,
--     DROP COLUMN IF EXISTS role_family,
--     DROP COLUMN IF EXISTS seniority,
--     DROP COLUMN IF EXISTS employment_type,
--     DROP COLUMN IF EXISTS metro,
--     DROP COLUMN IF EXISTS is_remote,
--     DROP COLUMN IF EXISTS is_genuine_role,
--     DROP COLUMN IF EXISTS qualified_at,
--     DROP COLUMN IF EXISTS qualified_hash;

-- ---------------------------------------------------------------------------
-- 1. Qualification tag columns. All nullable: NULL = not-yet-tagged (flag was
--    off when the job was ingested, or the best-effort tagger errored). Readers
--    must treat NULL as "unknown / don't pre-filter" so a tagging gap never
--    silently drops a job from a target's funnel.
-- ---------------------------------------------------------------------------
ALTER TABLE "public"."jobs"
    ADD COLUMN IF NOT EXISTS "is_us" boolean,
    ADD COLUMN IF NOT EXISTS "us_confidence" smallint,
    ADD COLUMN IF NOT EXISTS "role_family" "text",
    ADD COLUMN IF NOT EXISTS "seniority" "text",
    ADD COLUMN IF NOT EXISTS "employment_type" "text",
    ADD COLUMN IF NOT EXISTS "metro" "text",
    ADD COLUMN IF NOT EXISTS "is_remote" boolean,
    ADD COLUMN IF NOT EXISTS "is_genuine_role" boolean,
    ADD COLUMN IF NOT EXISTS "qualified_at" timestamp with time zone,
    ADD COLUMN IF NOT EXISTS "qualified_hash" "text";

COMMENT ON COLUMN "public"."jobs"."is_us" IS 'Qualification firewall (#60): true if the role is US-based (a multi-location role that includes ANY US location counts as US). NULL = not yet tagged.';
COMMENT ON COLUMN "public"."jobs"."us_confidence" IS 'Qualification firewall (#60): 0-100 model confidence in is_us. NULL = not yet tagged.';
COMMENT ON COLUMN "public"."jobs"."role_family" IS 'Qualification firewall (#60): coarse role function (engineering/data_ml/product/design/customer_experience/sales/marketing/finance/operations/people_hr/legal/other). NULL = not yet tagged.';
COMMENT ON COLUMN "public"."jobs"."seniority" IS 'Qualification firewall (#60): org level/scope (intern/entry/ic/senior_ic/manager/director/vp/exec/unknown) — NOT title keywords. NULL = not yet tagged.';
COMMENT ON COLUMN "public"."jobs"."employment_type" IS 'Qualification firewall (#60): full_time/contract/part_time/internship/temporary/unknown. NULL = not yet tagged.';
COMMENT ON COLUMN "public"."jobs"."metro" IS 'Qualification firewall (#60): primary metro/city string when identifiable, else NULL.';
COMMENT ON COLUMN "public"."jobs"."is_remote" IS 'Qualification firewall (#60): true if the role is remote-eligible. NULL = not yet tagged.';
COMMENT ON COLUMN "public"."jobs"."is_genuine_role" IS 'Qualification firewall (#60): false for talent-pool / "general application" / evergreen non-roles. NULL = not yet tagged.';
COMMENT ON COLUMN "public"."jobs"."qualified_at" IS 'Qualification firewall (#60): when the tagger last classified this row. NULL = never tagged.';
COMMENT ON COLUMN "public"."jobs"."qualified_hash" IS 'Qualification firewall (#60): sha256 over (title+company+location+description). Lets the tagger skip re-classifying unchanged rows on re-poll.';

-- ---------------------------------------------------------------------------
-- 2. CHECK constraints pin the enum domains so a bad tagger write (or a future
--    typo) fails loudly instead of poisoning a target's pre-filter. NULL is
--    allowed in every constraint (not-yet-tagged rows). Added NOT VALID + a
--    deferred VALIDATE so the brief ACCESS EXCLUSIVE lock from ADD CONSTRAINT
--    doesn't have to scan the whole table while holding it: NOT VALID takes the
--    lock only to register the constraint (no scan), VALIDATE re-checks existing
--    rows under a weaker SHARE UPDATE EXCLUSIVE lock that doesn't block writes.
--    Existing rows are all NULL here (columns just added), so VALIDATE is a
--    formality, but the pattern stays correct as the table grows.
--
--    Each ADD is guarded on pg_constraint (Postgres has no
--    ``ADD CONSTRAINT IF NOT EXISTS``) so re-running the file is a no-op; the
--    VALIDATE step is naturally idempotent (re-validating an already-valid
--    constraint is a cheap no-op).
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_role_family_check'
          AND conrelid = 'public.jobs'::regclass
    ) THEN
        ALTER TABLE "public"."jobs"
            ADD CONSTRAINT "jobs_role_family_check" CHECK (
                "role_family" IS NULL OR "role_family" IN (
                    'engineering', 'data_ml', 'product', 'design',
                    'customer_experience', 'sales', 'marketing', 'finance',
                    'operations', 'people_hr', 'legal', 'other'
                )
            ) NOT VALID;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_seniority_check'
          AND conrelid = 'public.jobs'::regclass
    ) THEN
        ALTER TABLE "public"."jobs"
            ADD CONSTRAINT "jobs_seniority_check" CHECK (
                "seniority" IS NULL OR "seniority" IN (
                    'intern', 'entry', 'ic', 'senior_ic', 'manager',
                    'director', 'vp', 'exec', 'unknown'
                )
            ) NOT VALID;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_employment_type_check'
          AND conrelid = 'public.jobs'::regclass
    ) THEN
        ALTER TABLE "public"."jobs"
            ADD CONSTRAINT "jobs_employment_type_check" CHECK (
                "employment_type" IS NULL OR "employment_type" IN (
                    'full_time', 'contract', 'part_time', 'internship',
                    'temporary', 'unknown'
                )
            ) NOT VALID;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_us_confidence_range_check'
          AND conrelid = 'public.jobs'::regclass
    ) THEN
        ALTER TABLE "public"."jobs"
            ADD CONSTRAINT "jobs_us_confidence_range_check" CHECK (
                "us_confidence" IS NULL
                OR ("us_confidence" >= 0 AND "us_confidence" <= 100)
            ) NOT VALID;
    END IF;
END $$;

ALTER TABLE "public"."jobs" VALIDATE CONSTRAINT "jobs_role_family_check";
ALTER TABLE "public"."jobs" VALIDATE CONSTRAINT "jobs_seniority_check";
ALTER TABLE "public"."jobs" VALIDATE CONSTRAINT "jobs_employment_type_check";
ALTER TABLE "public"."jobs" VALIDATE CONSTRAINT "jobs_us_confidence_range_check";

-- ---------------------------------------------------------------------------
-- 3. Partial index that serves the per-target pre-filter. A target screens its
--    live, qualified candidates by intrinsic facts before paying per-target LLM
--    cost — the hot shape is "live (archived_at IS NULL) jobs WHERE is_us = ?
--    AND role_family = ? AND seniority = ?". Leading columns match that
--    equality prefix; restricting to archived_at IS NULL keeps the index to the
--    live working set (dead jobs are never pre-filtered).
--
-- index-lock-ok: jobs carries a CREATE INDEX guard (#112) because the poller
--   writes it continuously, but at current beta scale (#101) the table is small
--   and this single partial-index build's brief SHARE lock is acceptable — the
--   same rationale and convention as 20260620140000_index_fit_status_log_scores.sql
--   and 20260620120000_db_hygiene_fk_indexes_and_anon_revoke.sql. The
--   CONCURRENTLY-at-scale rebuild stays tracked in #112 (CONCURRENTLY can't run
--   inside the txn `supabase db push` wraps each migration file in).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS "idx_jobs_qualification_prefilter"
    ON "public"."jobs" USING "btree" ("is_us", "role_family", "seniority")
    WHERE "archived_at" IS NULL;
