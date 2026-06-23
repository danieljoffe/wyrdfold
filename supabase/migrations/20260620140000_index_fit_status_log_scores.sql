-- #2 audit (index fit): add two missing hot-path indexes + drop an exact-dup.
--
-- index-lock-ok: status_log + scores are tiny at beta scale (#101); the brief
--   SHARE lock during these single small index builds is acceptable. The
--   CONCURRENTLY-rebuild-at-scale strategy stays tracked in #112 (CONCURRENTLY
--   can't run inside the txn `supabase db push` wraps each file in). Matches the
--   convention in 20260620120000_db_hygiene_fk_indexes_and_anon_revoke.sql.

-- M1 -- status_log per-user window read.
--   The #79 RLS read policy (20260616020000_status_log_user_attribution.sql)
--   filters every authenticated read by user_id; the insights window query
--   (services/insights.py _fetch_status_logs_window) is
--   `WHERE user_id = ? AND created_at >= ? AND created_at < ?`. No existing
--   index leads with user_id (the two existing ones lead with created_at /
--   posting_id), so the now-mandatory per-user filter can't be served by an
--   index. Add the leading-user_id composite.
CREATE INDEX IF NOT EXISTS idx_status_log_user_created
  ON public.status_log (user_id, created_at DESC);

-- M2 -- scores recency-decay list / pagination path.
--   The jobs-list recency fallback (routers/jobs.py _fetch_target_jobs_via_scores,
--   taken when recency_decay_enabled and sort=score) is
--   `WHERE target_id = ? AND excluded = false [AND score >= ?]
--    ORDER BY recency_score DESC, job_posting_id`.
--   The existing scores_target_recency_idx (target_id, recency_score DESC) omits
--   `excluded` (becomes a heap filter) and the job_posting_id tiebreaker (forces
--   a sort). Add the fully-covering composite.
--   NB the old index is intentionally NOT dropped here -- it still serves
--   target-only recency scans that don't filter `excluded`; drop it only after
--   pg_stat_user_indexes confirms it unused (tracked in #2).
CREATE INDEX IF NOT EXISTS idx_scores_target_excl_recency_jpid
  ON public.scores (target_id, excluded, recency_score DESC, job_posting_id);

-- M3 -- drop an exact-duplicate index. Safe regardless of usage: an identical
--   index remains to serve the same lookups. idx_prose_user_version ==
--   idx_experience_prose_user_version, both (user_id, version DESC) on
--   experience_prose_docs (a cold table -> unguarded; DROP INDEX keeps row data,
--   so it is not destructive DDL).
DROP INDEX IF EXISTS public.idx_prose_user_version;
