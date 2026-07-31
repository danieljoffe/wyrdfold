-- P0 (schema audit 2026-07-30): retire the sync_target_active trigger and
-- demote targets.is_active -> app_active (option A, re-semantics).
--
-- WHY: targets.is_active did double duty — a trigger-maintained cache of
-- "has an active member" for user targets, AND a manual floor for app-owned
-- catalog targets (#543, zero memberships). The trigger only knew the first
-- rule, so the catalog-search happy path (a user follows a catalog target →
-- from_input links with is_active=false → trigger recomputes EXISTS(active))
-- silently flipped the catalog target inactive and nothing ever reactivated
-- it. An activate→deactivate cycle clobbered it the same way. Full analysis:
-- .claude/docs/audit-wyrdfold-schema-debt-2026-07-30.md (P0) +
-- docs/decisions.md (2026-07-30 entry).
--
-- NEW SEMANTICS: app_active is ONLY the standing instance-sponsorship floor
-- (catalog/ops), written by the seed script / operator paths, never by user
-- actions. Pipeline-active is DERIVED at read time in crud.get_active /
-- crud.is_pipeline_active as: app_active OR EXISTS(active membership).
--
-- NOT backward-compatible (column rename): ships as a tight cutover with the
-- API deploy. The only readers are poller-internal + /targets responses.

-- 1. The trigger and its function go away entirely.
DROP TRIGGER IF EXISTS trg_sync_target_active ON public.user_targets;
DROP FUNCTION IF EXISTS public.sync_target_active();

-- 2. Rename + new default. Existing indexes follow the rename automatically.
ALTER TABLE public.targets RENAME COLUMN is_active TO app_active;
ALTER TABLE public.targets ALTER COLUMN app_active SET DEFAULT false;

-- 3. Data reset: the flag now means ONLY the manual floor. Any target that
-- has memberships derives its pipeline state from them — reset those to
-- false so the only remaining `true` rows are the link-free catalog floor.
-- (Prod 2026-07-31: leaves the 5 seeded catalog targets true; the one
-- active user target is covered by the EXISTS arm at read time.)
UPDATE public.targets t
   SET app_active = false
 WHERE t.app_active
   AND EXISTS (SELECT 1 FROM public.user_targets ut WHERE ut.target_id = t.id);

COMMENT ON COLUMN public.targets.app_active IS
  'Standing instance-sponsorship floor (app-owned catalog / operator): the '
  'instance ingests+triages this target regardless of memberships. NEVER '
  'written by user actions. Pipeline-active is derived at read time as '
  'app_active OR EXISTS(user_targets.is_active) — see crud.get_active. '
  'Replaces the trigger-synced is_active (dropped 2026-07-31, schema audit P0).';

-- 4. Index hygiene: keep names truthful (definitions are unchanged by the
-- column rename; only the identifiers are stale).
ALTER INDEX IF EXISTS idx_job_targets_active RENAME TO idx_targets_app_active_created;
ALTER INDEX IF EXISTS idx_targets_active_only RENAME TO idx_targets_app_active_only;

-- 5. The operator funnel view referenced t.is_active. Rebuild it exposing
-- the new column AND the derived pipeline_active, so the operator ruler
-- reflects what the pipeline actually iterates. DROP+CREATE (not OR REPLACE)
-- because the column set changes.
DROP VIEW IF EXISTS public.target_funnel;
CREATE VIEW public.target_funnel
WITH (security_invoker = true) AS
SELECT
  t.id AS target_id,
  t.app_active,
  (t.app_active OR EXISTS (
     SELECT 1 FROM public.user_targets ut
      WHERE ut.target_id = t.id AND ut.is_active
  )) AS pipeline_active,
  (
    SELECT kw.value
    FROM jsonb_array_elements_text(t.search_keywords) WITH ORDINALITY kw(value, ord)
    ORDER BY kw.ord
    LIMIT 1
  ) AS domain,
  count(j.id) AS graded,
  count(j.id) FILTER (WHERE s.promising) AS promising,
  count(j.id) FILTER (WHERE s.score > 0) AS phase2,
  count(j.id) FILTER (WHERE s.score >= 30) AS ge30,
  count(j.id) FILTER (WHERE s.score >= 50) AS ge50,
  count(j.id) FILTER (WHERE s.score >= 75) AS ge75,
  coalesce(max(s.score) FILTER (WHERE j.id IS NOT NULL), 0) AS max_score
FROM public.targets t
LEFT JOIN public.scores s ON s.target_id = t.id
LEFT JOIN public.jobs j ON j.id = s.job_posting_id AND j.archived_at IS NULL
GROUP BY t.id, t.app_active, t.search_keywords;

REVOKE ALL ON public.target_funnel FROM anon, authenticated;
GRANT SELECT ON public.target_funnel TO service_role;

COMMENT ON VIEW public.target_funnel IS
  'Operator-only per-target relevance funnel over live jobs (issue #60). '
  'Cross-tenant aggregate; SELECT granted to service_role only. '
  'pipeline_active mirrors crud.get_active''s derived predicate.';
