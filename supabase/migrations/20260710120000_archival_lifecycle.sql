-- Archival lifecycle (UX/IA report §5, Stage 1 + Stage 2 A/B).
--
-- Stage 1 (30d): a scheduled sweep sets ``archived_at`` on stale, untouched
-- jobs — reversible, and the default list views already exclude archived rows.
-- Stage 2 (60d): the sweep either HARD-DELETES long-archived rows that are
-- pure noise (no user engagement, no graded history, delisted by their
-- source — FK children cascade) or TOMBSTONES them (``purged_at`` stamped +
-- heavy payload stripped, row kept so the poller can refuse re-insert and
-- Insights history survives).
--
-- ``purged_at`` semantics: NOT NULL means "tombstone" — the row is retained
-- only as a re-ingestion guard + history stub. Purged rows are already
-- invisible to live views (they are archived); the archived VIEW must also
-- exclude them (purged_at IS NULL) since their payload is gone.

ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS purged_at timestamptz;

COMMENT ON COLUMN public.jobs.purged_at IS
  'Tombstone stamp (archival Stage 2): payload stripped, row kept to block poller re-insert. NULL = live or merely archived.';

-- Sweep support: the Stage-1 scan is "oldest live rows" and the Stage-2 scan
-- is "oldest archived, not yet purged" — both partial so they stay tiny.
-- index-lock-ok: jobs is ~31k rows; both indexes are partial (live subset
-- ~5k / archived subset ~26k of narrow columns), so each build holds the
-- write lock well under a second — cheaper than two extra migration files.
CREATE INDEX IF NOT EXISTS idx_jobs_archive_sweep
  ON public.jobs (created_at)
  WHERE archived_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_purge_sweep
  ON public.jobs (archived_at)
  WHERE archived_at IS NOT NULL AND purged_at IS NULL;
