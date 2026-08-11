-- Wire-up backfill (schema audit, decided 2026-07-31): the qualification
-- firewall now archives ``is_genuine_role = false`` rows at tag time
-- (talent-pool / "general application" / evergreen collectors are not jobs).
-- This one-shot catches the rows tagged before the wiring existed — prod had
-- 139 of them live and serving in public search.
--
-- Reversible (archived_at flag, not destructive); the jobs→scores denorm
-- trigger syncs job_is_live for affected scores rows automatically.
UPDATE public.jobs
   SET archived_at = now(),
       updated_at = now()
 WHERE is_genuine_role = false
   AND archived_at IS NULL;
