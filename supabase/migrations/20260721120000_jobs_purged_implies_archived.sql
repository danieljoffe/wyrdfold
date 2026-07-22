-- Hardening review 2026-07-21 (DB-F7): make "purged implies archived" a real
-- constraint instead of a load-bearing convention.
--
-- get_target_jobs (20260708040000) excludes payload-stripped tombstones only
-- transitively: it filters archived_at IS NULL, and the Stage-2 sweep is coded
-- to purge only already-archived rows. pipeline_counts later added an explicit
-- purged_at IS NULL arm "for parity"; the list RPC never did. If any future
-- writer ever stamps purged_at on an unarchived row (or clears archived_at on a
-- tombstone), title-less/URL-less husks would render in the per-target list with
-- no error anywhere. A CHECK makes the invariant impossible to violate.
--
-- Prod verified 0 rows with purged_at set and archived_at NULL (2026-07-21), so
-- this validates instantly at current table size.

ALTER TABLE public.jobs
  ADD CONSTRAINT jobs_purged_implies_archived
  CHECK (purged_at IS NULL OR archived_at IS NOT NULL);
