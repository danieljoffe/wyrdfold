-- #665 prep: `idx_scores_live_dedup` must carry `recency_score`.
--
-- The floor moves from `s.score` to `s.recency_score` (see the sibling
-- migration 20260809090000). Without the column in this index's INCLUDE list,
-- the dedup CTE in `get_cross_target_jobs` cannot answer the filter from the
-- index and degrades from an Index Only Scan to a Bitmap Heap Scan. Measured on
-- the real corpus at p_min_score=70:
--
--     old index + recency floor : Bitmap Heap Scan, 5,822 heap blocks, 371ms
--     new index + recency floor : Index Only Scan,     90 heap fetches,  12.8ms
--
-- (12.8ms is a fresh-index best case — heap fetches climb as writes accumulate
-- and the visibility map goes stale. The durable win is the plan shape.)
--
-- KEY and WHERE are unchanged; this only widens INCLUDE, so the new index is a
-- strict superset of the old one and serves every query the old one served.
-- That is what made the prod swap safe to do before the code that needs it.
--
-- Applied to PROD out-of-band via `CREATE INDEX CONCURRENTLY` (2026-08-08,
-- built as `..._v2`, old index dropped with `DROP INDEX CONCURRENTLY`, then
-- renamed into place) because CONCURRENTLY cannot run inside the transaction
-- `supabase db push` wraps each file in (#112) — and recorded in prod's
-- migration ledger as already-applied. This file exists for fresh/staging
-- parity. Do NOT run it against prod: the ledger row makes `db push` skip it,
-- and running it anyway would rebuild the live index under a write lock.
--
-- index-lock-ok: prod got the ONLINE CREATE/DROP INDEX CONCURRENTLY treatment
-- out-of-band and skips this file via the ledger (#112); the plain DDL below
-- only ever runs on a FRESH/staging DB where `scores` is empty or tiny, so its
-- brief build lock is a non-issue.
DROP INDEX IF EXISTS public.idx_scores_live_dedup;
CREATE INDEX IF NOT EXISTS idx_scores_live_dedup
  ON public.scores
  USING btree (target_id, score DESC, job_posting_id DESC)
  INCLUDE (scoring_status, is_graded, job_role_family, job_first_seen_at, recency_score)
  WHERE (job_is_live AND (NOT excluded));
