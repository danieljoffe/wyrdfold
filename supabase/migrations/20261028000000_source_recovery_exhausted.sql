-- Park job boards that auto-recovery has failed to revive three times.
--
-- In plain terms: retirement (20260930000000) only fires on PROOF a board is
-- dead -- a 404 plus a re-detection that conclusively finds nothing. Some dead
-- boards can never produce that proof: a Workday site whose careers page
-- itself answers 500 (every fetch is a 422), or a Greenhouse 404 whose
-- re-detection is inconclusive. Those cycle forever -- disabled after 10
-- failures (alert), re-enabled 24h later, disabled again (alert) -- ~25 boards
-- and ~5 Sentry events a day in prod on 2026-10-28.
--
-- `auto_disables` counts the times the poller auto-disabled a source on a
-- board-specific rejection (HTTP 400/404/410/422) since its last clean poll.
-- Once a source has been through three such recovery rounds and is rejected
-- again, the poller retires it with reason 'recovery_exhausted' instead of
-- disabling it, and auto-recovery stops reviving it. Outage-shaped disables
-- (5xx, timeouts, 429, 401/403) neither count nor park. A clean poll, an ATS
-- re-point, or an operator re-enable resets the counter.
--
-- Reversible per row: an operator enabling the source clears the retirement
-- and the counter. Bulk undo:
--   UPDATE public.sources
--      SET enabled = true, consecutive_failures = 0, disabled_at = NULL,
--          retired_at = NULL, retired_reason = NULL, auto_disables = 0
--    WHERE retired_reason = 'recovery_exhausted';
--
-- Additive: the column defaults to 0 (no backfill -- the rows already in the
-- loop park themselves after three more rounds), and the CHECK only widens.
--
-- Manual down (after the bulk undo above):
--   ALTER TABLE public.sources DROP CONSTRAINT sources_retired_reason_check;
--   ALTER TABLE public.sources ADD CONSTRAINT sources_retired_reason_check CHECK (
--     (retired_at IS NULL AND retired_reason IS NULL)
--     OR (retired_at IS NOT NULL AND retired_reason IS NOT NULL
--         AND retired_reason IN ('dead_board', 'duplicate')));
--   ALTER TABLE public.sources DROP COLUMN IF EXISTS auto_disables;

ALTER TABLE "public"."sources"
    ADD COLUMN IF NOT EXISTS "auto_disables" integer NOT NULL DEFAULT 0;

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
            AND "retired_reason" IN ('dead_board', 'duplicate', 'recovery_exhausted'))
    );

COMMENT ON COLUMN "public"."sources"."auto_disables" IS 'Times the poller auto-disabled this source on a board-specific rejection (HTTP 400/404/410/422) since its last clean poll. Reset by a clean poll, an ATS re-point, or an operator re-enable. Past source_park_after_failed_recoveries the next disable retires the source (recovery_exhausted).';
COMMENT ON COLUMN "public"."sources"."retired_reason" IS 'Why the source was retired: dead_board, duplicate, or recovery_exhausted (failed again after the configured number of auto-recovery rounds). NULL exactly when retired_at is NULL.';
