-- Slim `idx_scores_target_excl_score_jpid`: drop the dead `axis_scores` from its
-- INCLUDE. That index-only plan was replaced by `idx_scores_live_dedup` (which
-- INCLUDEs the denorm cols, NOT `axis_scores`), so the JSONB in this index's
-- leaf was pure write-amplification on the poller-churned `scores` table
-- (~13 MB -> ~5 MB on prod). The KEY is unchanged, so it still serves the
-- score-sorted keyset path (its prod scans, via an index-only scan).
--
-- Applied to PROD out-of-band via `CREATE/DROP INDEX CONCURRENTLY` (2026-07-20)
-- because CONCURRENTLY can't run inside `supabase db push`'s transaction (#112),
-- and recorded in prod's migration ledger as already-applied. This file exists
-- for fresh/staging parity: a non-concurrent DROP+CREATE is fine on an
-- empty/small `scores` table. Do NOT run it against prod (the ledger row makes
-- `db push` skip it there; running it would non-concurrently rebuild the live
-- index under a brief write lock).
--
-- index-lock-ok: prod got the ONLINE CREATE/DROP INDEX CONCURRENTLY treatment
-- out-of-band and skips this file via the ledger (#112); the plain CREATE below
-- only ever runs on a FRESH/staging DB where `scores` is empty, so its brief
-- build lock is a non-issue.
DROP INDEX IF EXISTS public.idx_scores_target_excl_score_jpid;
CREATE INDEX IF NOT EXISTS idx_scores_target_excl_score_jpid
  ON public.scores
  USING btree (target_id, excluded, score DESC, job_posting_id DESC)
  INCLUDE (scoring_status);
