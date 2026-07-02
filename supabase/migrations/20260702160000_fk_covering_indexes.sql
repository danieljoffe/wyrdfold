-- Covering indexes for the account-cascade user_id FKs (advisor 0001).
--
-- #173/#176 added user_id FKs to auth.users on analyses, contribution_votes,
-- and reference_jds. None of these tables has user_id as a LEADING index
-- column — the existing composites lead with other columns
-- (analyses_cache_key_unique → job_posting_id; idx_reference_jds_target_user →
-- target_id; contribution_votes_user_ref_key → reference_jd_id) — so Postgres
-- has no covering index for the FK.
--
-- Why it matters despite tiny tables today: the FK's ON DELETE action
-- (CASCADE for analyses/contribution_votes, SET NULL for reference_jds) fires
-- on the #29 erasure path. Deleting an auth.users row scans each referencing
-- table for matching user_id; unindexed, that is a seq scan per table per
-- deletion. Indexing keeps account deletion O(rows-for-that-user), not
-- O(all-rows) — the property that matters as the catalog grows.
--
-- IF NOT EXISTS: safe to re-run; harmless if a future composite already leads
-- with user_id.

create index if not exists idx_analyses_user_id
    on public.analyses (user_id);

create index if not exists idx_contribution_votes_user_id
    on public.contribution_votes (user_id);

create index if not exists idx_reference_jds_user_id
    on public.reference_jds (user_id);
