-- #2 audit — live-DB advisor pass (2026-06-29). Three findings, each verified
-- against the live project's `proacl` / `pg_indexes` (NOT the cached advisor —
-- which still reports the already-fixed leaked-password and RLS-initplan items,
-- so it is stale; the items below were confirmed live):
--
--   A. The three `user_*` score-write SECURITY DEFINER RPCs still carry an
--      EXECUTE grant for `anon`. #6 R2 (20260621140000 / 20260621150000)
--      revoked PUBLIC and granted authenticated + service_role — but Supabase's
--      default privileges ALSO grant EXECUTE to `anon` on every new function,
--      and `REVOKE ... FROM PUBLIC` does not remove that explicit per-role
--      grant. Live `proacl` shows `anon=X/postgres` on all three. Their in-body
--      ownership guard is `IF auth.uid() IS NOT NULL AND NOT EXISTS (...)`, so
--      an anon caller (auth.uid() = NULL) skips the check entirely and is
--      treated like the trusted service-role/operator path. Net: a holder of
--      the public anon key could, unauthenticated, upsert/override arbitrary
--      rows in the shared `scores` catalog and stamp `jobs.llm_analysis_id`.
--      Revoke anon; keep `authenticated` (guarded to its own uid) and
--      `service_role` (the backend's real caller — analysis.py / jobs.py /
--      target_scoring.py call these on the JWT or service-role client, never anon).
--
--   B. Three trigger / event-trigger DEFINER functions still hold GRANT ALL to
--      PUBLIC/anon/authenticated (audit AUDIT_2026-06-22.md finding I4):
--      `rls_auto_enable` (RETURNS event_trigger), `set_job_feedback_updated_at`
--      and `set_target_learning_log_updated_at` (RETURN trigger). Postgres
--      blocks direct RPC invocation of trigger/event-trigger functions, so
--      these are exploitability-inert — but the standing grants break the
--      "every SECURITY DEFINER function is locked down" invariant. Revoke for
--      consistency; the trigger machinery and `service_role` are unaffected.
--
--   C. `prescan_shadow.job_posting_id` (FK → public.jobs) has no covering index
--      — the table's only indexes are its `id` PK and a (target, observed)
--      index — so FK validation / parent-row deletes seq-scan it. Add one.
--
-- All statements are idempotent (REVOKE is a no-op once revoked; CREATE INDEX
-- IF NOT EXISTS). Non-destructive (no row-data DDL); `prescan_shadow` is not a
-- hot table, so the plain (non-CONCURRENTLY) index build is allowed by
-- tests/test_migration_safety.py.

-- A. Score-write RPCs — drop the leftover `anon` grant only.
REVOKE ALL ON FUNCTION public.user_upsert_score(jsonb)
  FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid)
  FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.user_set_scores_included(uuid, uuid[])
  FROM PUBLIC, anon;

-- B. Trigger / event-trigger DEFINER functions — full lockdown (keep service_role).
REVOKE ALL ON FUNCTION public.rls_auto_enable()
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.set_job_feedback_updated_at()
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.set_target_learning_log_updated_at()
  FROM PUBLIC, anon, authenticated;

-- C. Covering index for the prescan_shadow → jobs foreign key.
CREATE INDEX IF NOT EXISTS idx_prescan_shadow_job_posting
  ON public.prescan_shadow (job_posting_id);
