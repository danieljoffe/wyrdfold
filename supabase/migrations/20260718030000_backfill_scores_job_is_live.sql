-- db(M2): fold the scores denorm backfill into the migration history.
--
-- 20260717040000 added the denorm columns (job_is_live, is_graded,
-- job_role_family, job_first_seen_at) + the sync triggers, but backfilled the
-- ~30k pre-existing rows with a SEPARATE THROTTLED SCRIPT that lives OUTSIDE
-- the migration history. So a migrations-only replay (DR restore, a staging
-- clone seeded before the script ran, a future re-clone) leaves those rows
-- with job_is_live = NULL. A NULL job_is_live is:
--   * absent from the partial index idx_scores_live_dedup
--     (WHERE job_is_live AND NOT excluded), and
--   * absent from get_cross_target_jobs' `best` CTE (WHERE s.job_is_live —
--     NULL is not TRUE),
-- so a live job silently DOES NOT APPEAR in the cross-target list, with no
-- error. This migration makes the invariant enforce-able from the history.
--
-- DERIVATION mirrors scores_sync_denorm (20260717040000:60-71): the three
-- jobs-derived columns come from the parent job, is_graded from the row's own
-- axis_scores. `is_us IS NOT FALSE` (NULL/TRUE => live-eligible) matches the
-- trigger exactly.
--
-- SENTINEL. Gated on `job_is_live IS NULL` — the trigger's own "needs-backfill"
-- marker (20260717040000:65). This is the ONLY correct sentinel: job_role_family
-- and job_first_seen_at can be legitimately NULL on a fully-populated row (a job
-- with no role_family / an old row), so gating on those would wrongly re-touch
-- live rows. Verified on prod 2026-07-18: job_role_family IS NULL on 36,436 rows
-- while job_is_live IS NULL on 0 — proof the two are not interchangeable.
--
-- IDEMPOTENT + PROD-SAFE. Prod was already backfilled by the out-of-band script,
-- so this matches 0 rows on prod (verified 2026-07-18: count WHERE job_is_live
-- IS NULL => 0; is_graded / job_first_seen_at also 0 NULL). Re-running is a
-- no-op. Backward-compatible with the running code (migrations-first is safe).
-- The UPDATE fires scores_sync_denorm (BEFORE UPDATE), but because the SET makes
-- job_is_live non-NULL first, the trigger's own jobs-lookup branch is skipped
-- and it only re-affirms is_graded to the identical value — consistent, and only
-- on the gated (0-on-prod) rows.

UPDATE public.scores s
SET
    job_is_live = (jp.archived_at IS NULL AND jp.purged_at IS NULL AND jp.is_us IS NOT FALSE),
    job_role_family = jp.role_family,
    job_first_seen_at = jp.first_seen_at,
    is_graded = (s.axis_scores IS NOT NULL)
FROM public.jobs jp
WHERE jp.id = s.job_posting_id
  AND s.job_is_live IS NULL;
