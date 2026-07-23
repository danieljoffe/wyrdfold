-- #457 follow-up — drop the orphaned 11-arg get_cross_target_jobs overload.
--
-- 20260723000000 added a p_weights param via CREATE OR REPLACE. Because that
-- CHANGES the argument signature (11 -> 12 args), Postgres created a SECOND
-- overload rather than replacing the existing 11-arg function — leaving both:
--   get_cross_target_jobs(uuid[],int,text,text,text,text,bool,int,int,uuid,bool)
--   get_cross_target_jobs(uuid[],int,text,text,text,text,bool,int,int,uuid,bool,jsonb)
--
-- An 11-arg call (the pre-#457 code, e.g. during a deploy window) then matches
-- BOTH candidates — the 12-arg one via p_weights's default — which Postgres
-- rejects as "function is not unique". Drop the old 11-arg signature so every
-- caller resolves to the single 12-arg function; an 11-arg call binds it with
-- p_weights defaulting to '{}' (the prior, un-weighted behavior). This is the
-- genuinely backward-compatible state the migration should have produced.
--
-- On a fresh reset this runs right after 20260723000000, so the two overloads
-- coexist only transiently within the migration run (nothing calls the RPC
-- between them). Idempotent via IF EXISTS.
DROP FUNCTION IF EXISTS public.get_cross_target_jobs(
    uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean
);
