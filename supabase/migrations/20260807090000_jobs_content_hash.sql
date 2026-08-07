-- #642: change-detection for the poller's per-cycle content refresh.
--
-- pg_stat evidence (2026-08-07, small-instance IO exhaustion incidents):
-- jobs carried ~3.16M updates over ~50k ever-inserted rows (~63 rewrites
-- per row) and scores ~7.3M updates over ~207k (~35x), because every poll
-- cycle re-upserts every KNOWN row's full payload (description_html TOAST
-- included) and re-runs stage-2 scoring on it — even when nothing changed.
-- The redundant rewrites fed 700+ autovacuum runs on scores alone and the
-- TOAST vacuum grind behind the disk-IO exhaustion incidents.
--
-- ``content_hash`` stores a sha256 over the poller-refreshable payload
-- (title / location / sanitized description / url / posted-at / salary).
-- Unchanged known rows skip both the jobs rewrite and downstream
-- rescoring. Nullable on purpose: rows hash lazily on their next real
-- upsert (NULL never matches, so the first post-migration cycle writes
-- once and stamps). No index — compared in-app via the per-source
-- known-ids read.
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS content_hash text;

COMMENT ON COLUMN public.jobs.content_hash IS
  'sha256 of the poller-refreshable payload; unchanged known rows skip the per-cycle rewrite + rescore (#642)';
