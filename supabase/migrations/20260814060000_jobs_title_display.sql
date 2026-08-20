-- Display titles (ux-sweep 2026-08-12 §B1 follow-through).
--
-- jobs.title and jobs.company_name feed the poller's content-dedupe key
-- (_content_dedupe_key), so stored titles are never rewritten. The cleaned
-- display form lives in this ADDITIVE column instead: populated at ingest by
-- app/services/titles.clean_title_display, NULL whenever the raw title needs
-- no repair (serving falls back with coalesce/??). Backfill is a separate
-- one-shot script (scripts/backfill_title_display.py), run post-migration.
--
-- No index: the column is never filtered or sorted on — display only.
ALTER TABLE public.jobs
    ADD COLUMN IF NOT EXISTS title_display text;

COMMENT ON COLUMN public.jobs.title_display IS
    'Cleaned display form of title (deterministic, app/services/titles.py). '
    'NULL = raw title is already presentable. Never used for dedupe/search.';
