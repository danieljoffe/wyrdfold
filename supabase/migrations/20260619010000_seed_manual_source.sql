-- Seed the "manual" pseudo-source row.
--
-- POST /jobs/manual upserts user-pasted job postings under a fixed
-- pseudo-source (MANUAL_SOURCE_ID = 00000000-0000-4000-a000-000000000001 in
-- app/services/extract.py) so they satisfy the NOT-NULL
-- job_postings.source_id FK without belonging to any real polled board. If
-- this row is absent, the upsert violates job_postings_source_id_fkey and the
-- request 500s with a raw Postgres error. This migration makes the row exist
-- in every environment.
--
-- enabled = FALSE so the poller never tries to fetch this fake board.
-- poll_interval_minutes is set to the max allowed (10080 = weekly) to stay
-- inside sources_poll_interval_minutes_check (5..10080); it is moot while the
-- source is disabled. board_token is a sentinel that won't collide with any
-- real Greenhouse/Lever token (the column has a UNIQUE constraint).
--
-- Idempotent: ON CONFLICT (id) DO NOTHING makes re-runs a no-op, and the
-- manual-add path also self-heals this row at request time, so a fresh DB or
-- a wiped row recovers without this migration.
INSERT INTO public.sources (
  id,
  provider,
  board_token,
  company_name,
  enabled,
  poll_interval_minutes,
  consecutive_failures
)
VALUES (
  '00000000-0000-4000-a000-000000000001',
  'manual',
  '__manual__',
  'Manually Added',
  FALSE,
  10080,
  0
)
ON CONFLICT (id) DO NOTHING;
