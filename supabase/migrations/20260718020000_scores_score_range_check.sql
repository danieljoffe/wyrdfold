-- db(L3): give scores.score the 0-100 range CHECK that jobs.score already has.
--
-- jobs.score carries CONSTRAINT jobs_score_check CHECK (score >= 0 AND score
-- <= 100) (remote_schema:660); scores.score (DEFAULT 0 NOT NULL) had none, so
-- any writer could store out-of-domain values (negatives, 2147483647). This
-- closes the data-quality gap and narrows the blast radius of the shared-score
-- write path (locked down to service_role in 20260718010000) — defense in
-- depth: even a service-role bug can't now persist an out-of-range score.
--
-- PROD VERIFIED CLEAN before adding (2026-07-18, `supabase db query --linked`):
--   SELECT count(*) FROM scores WHERE score < 0 OR score > 100  => 0
--   SELECT min(score), max(score) FROM scores                   => 0, 100
-- across 78,779 rows, so the constraint validates without a rewrite and
-- without failing. It is added VALID (not NOT VALID) so existing rows are
-- enforced too: `supabase db push` wraps each migration file in a single
-- transaction, so a NOT VALID + VALIDATE split would give no lock advantage
-- here (both would run in the same txn). The validating scan of ~79k rows
-- takes a brief ACCESS EXCLUSIVE lock — apply in a poll-quiet window like the
-- other scores index/constraint changes (M1); the poller retries transient
-- write failures.
--
-- Idempotent (guarded on pg_constraint) so a partial replay is safe.

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'scores_score_check'
          AND conrelid = 'public.scores'::regclass
    ) THEN
        ALTER TABLE "public"."scores"
            ADD CONSTRAINT "scores_score_check" CHECK (
                "score" >= 0 AND "score" <= 100
            );
    END IF;
END $$;
