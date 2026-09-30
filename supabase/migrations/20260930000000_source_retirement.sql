-- Retire job boards that are proven dead, so auto-recovery stops reviving them.
--
-- In plain terms: when a board fails 10 polls in a row the poller switches it
-- off, and 24 hours later auto-recovery switches it back on — that is what
-- keeps an ATS-wide outage from taking ingestion down forever. But a board
-- that is GONE (the ATS answers 404 and no other board can be found for the
-- company), or a duplicate row whose company is already polled through
-- another row, fails its 10 polls again, is switched off again, and alerts
-- again. Forever. In prod on 2026-09-30 that loop held 48 sources and had
-- raised ~110 separate Sentry issues in a month.
--
-- `retired_at` marks the rows where the poller had PROOF, not just failures:
--   * 'dead_board' — the poller's own fetch got an HTTP 404, the board still
--     fails a direct probe, and no other live board exists for the company.
--   * 'duplicate'  — the company's live board is already owned by another
--     source row.
-- Auto-recovery skips retired rows. Timeouts, 5xx and every other ambiguous
-- failure never retire, so an outage still recovers on its own.
--
-- Reversible per row: an operator enabling the source clears both columns.
-- Bulk undo (e.g. if a bad deploy made every board 404):
--   UPDATE public.sources
--      SET enabled = true, consecutive_failures = 0, disabled_at = NULL,
--          retired_at = NULL, retired_reason = NULL
--    WHERE retired_at > '<bad deploy time>';
--
-- Additive and nullable, no backfill: the rows already in the loop retire
-- themselves on their next trip past the threshold.
--
-- Manual down:
--   ALTER TABLE public.sources
--     DROP COLUMN IF EXISTS retired_reason,
--     DROP COLUMN IF EXISTS retired_at;

ALTER TABLE "public"."sources"
    ADD COLUMN IF NOT EXISTS "retired_at" timestamp with time zone,
    ADD COLUMN IF NOT EXISTS "retired_reason" "text";

ALTER TABLE "public"."sources"
    DROP CONSTRAINT IF EXISTS "sources_retired_reason_check";
ALTER TABLE "public"."sources"
    ADD CONSTRAINT "sources_retired_reason_check"
    CHECK (
        ("retired_at" IS NULL AND "retired_reason" IS NULL)
        -- IS NOT NULL is load-bearing: `NULL IN (...)` is NULL, and a CHECK
        -- passes on NULL, so without it a retirement with no reason slips in.
        OR ("retired_at" IS NOT NULL
            AND "retired_reason" IS NOT NULL
            AND "retired_reason" IN ('dead_board', 'duplicate'))
    );

COMMENT ON COLUMN "public"."sources"."retired_at" IS 'When the poller retired this source on proof it is dead (404 + nothing found) or a duplicate of another source. Auto-recovery never re-enables a retired source. NULL = not retired. Cleared when an operator re-enables the source.';
COMMENT ON COLUMN "public"."sources"."retired_reason" IS 'Why the source was retired: dead_board or duplicate. NULL exactly when retired_at is NULL.';
