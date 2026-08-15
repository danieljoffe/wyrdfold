-- Skill search: index the catalog-wide skill facts so `/search?skill=react`
-- is a real filter rather than a scan.
--
-- The column itself already exists (20260815000000, the Phase-2 harvest). What
-- changed is who fills it: the qualification tagger now writes it for EVERY
-- newly-tagged job (target-independent, exactly one pass per job, 100% of the
-- live corpus), where the harvest only ever covered the graded slice. That
-- makes corpus-wide skill filtering possible, which makes an index necessary.
--
-- GIN with jsonb_path_ops: the only query shape is containment
-- (`skills_required @> '["react"]'`), and jsonb_path_ops is smaller and faster
-- than the default jsonb_ops for exactly that operator. It does NOT support
-- key-existence queries — acceptable, since the values are a flat string array
-- and no surface asks "does this row have any skills?" as an indexed question.
--
-- additive: index only, no column or data change. Safe to apply before or
-- after the code deploys (without it the filter still returns correct rows,
-- just via a scan of the candidate window).
--
-- index-lock-ok: brief write lock accepted. `jobs` IS hot (the poller upserts
-- every cycle), so this is a conscious call, not an oversight: the table is
-- ~57k rows and `skills_required` is NULL on all but ~241 of them (the column
-- shipped days ago, forward-fill only), so the GIN build is a sub-second scan
-- with almost nothing to index. CONCURRENTLY is not available on the
-- txn-wrapped `supabase db push` path (repo convention + #112), and the
-- alternative — waiting until the column is dense — means building the index
-- when it is EXPENSIVE rather than now while it is trivially cheap.

CREATE INDEX idx_jobs_skills_required
    ON public.jobs USING gin (skills_required jsonb_path_ops);
