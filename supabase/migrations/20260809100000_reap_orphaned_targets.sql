-- #667: "delete" a target and the row survives, unreachable, forever.
--
-- WHAT HAPPENS TODAY. `DELETE /api/targets/{id}` correctly drops only the
-- caller's MEMBERSHIP — targets are a shared catalog (`find_matching_target`
-- dedups by normalized label), so hard-deleting the row would cascade away every
-- co-follower's scores/feedback/analyses. That per-user unlink is the audit-#29
-- H1 fix and must stay.
--
-- But nothing ever removes the row once the LAST follower leaves. The endpoint is
-- membership-scoped, so from that moment it answers 404 to everyone, including
-- the person who created it. The row is unreachable by any supported path — and
-- it keeps every score row it ever accumulated.
--
-- Prod, 2026-08-08: 2 orphans (3 more were e2e scratch rows, cleaned by hand).
--     "Hadrian - Fullstack Software Engineer"  6,163 score rows
--     "SWE Infrastructure Specialist (Java)"       1 score row
-- 6,163 rows is ~3.8% of the whole 162k `scores` table, owned by nobody, on an
-- instance whose recurring performance problem is disk IO. THAT is the cost —
-- not the catalog-search clutter, which is real but minor (the labels are
-- ordinary role titles).
--
-- The comment on `delete_target` claims "the user_targets AFTER-DELETE trigger
-- deactivates the target once its last active follower leaves". That trigger was
-- `sync_target_active`, and it was DROPPED by 20260731090000 when `is_active`
-- became `app_active`. The comment is stale and the mechanism is gone — which is
-- why the orphans accumulated. It is corrected in this change.
--
-- WHY AN RPC AND NOT TWO POSTGREST CALLS. The guard is "no memberships remain",
-- which is a NOT EXISTS subquery PostgREST cannot express. Doing it as
-- count-then-delete from Python opens a window where a co-follower links the
-- target between the two calls and we delete a row someone is now using. A
-- single guarded DELETE is atomic: the NOT EXISTS is evaluated against the same
-- snapshot as the delete.

CREATE OR REPLACE FUNCTION public.reap_orphaned_target(p_target_id uuid)
RETURNS boolean
LANGUAGE sql
VOLATILE
SET search_path TO 'public', 'pg_catalog'
AS $function$
  WITH gone AS (
    DELETE FROM public.targets t
    WHERE t.id = p_target_id
      -- Ops/catalog sponsorship is a standing floor that outlives followers
      -- (#543). A seeded catalog target legitimately has zero memberships and
      -- must never be reaped.
      AND t.app_active IS NOT TRUE
      -- The whole guard: someone still follows it => not orphaned, hands off.
      -- Evaluated in the same snapshot as the DELETE, so a concurrent link
      -- cannot slip in between the check and the removal.
      AND NOT EXISTS (
        SELECT 1 FROM public.user_targets ut WHERE ut.target_id = t.id
      )
    RETURNING t.id
  )
  SELECT EXISTS (SELECT 1 FROM gone);
$function$;

COMMENT ON FUNCTION public.reap_orphaned_target(uuid) IS
  'Delete a target iff it has no memberships left and no ops sponsorship. '
  'Called after a user unlinks (#667). Cascades to scores/feedback/analyses/'
  'learning via their ON DELETE CASCADE FKs. Returns whether a row was removed.';

-- One-off pass for the backlog these orphans accumulated before the reap
-- existed. Same predicate as the function, so it can only touch rows that are
-- already unreachable through the API. Cascades clear their scores.
--
-- NOTE: this is an irreversible delete of real (if unreachable) rows —
-- ~6,164 score rows in prod at time of writing. It is the point of the change:
-- the user asked for those targets to be deleted and they were not.
DELETE FROM public.targets t
WHERE t.app_active IS NOT TRUE
  AND NOT EXISTS (SELECT 1 FROM public.user_targets ut WHERE ut.target_id = t.id);
