-- SEC-H2 (audit 2026-07-18): lock the user_* score-write RPCs to service_role.
--
-- user_upsert_score / user_set_scores_included / user_apply_score_blend are
-- SECURITY DEFINER functions that write the SHARED `scores` catalog, and each
-- is EXECUTE-granted to `authenticated`. The Supabase anon key ships in the
-- browser, so any logged-in user can call them directly via PostgREST. Their
-- in-DB check only verifies the caller *follows* the target — but targets are a
-- SHARED, ownerless catalog (anyone can follow any target), so a follower of a
-- popular target can zero/exclude its jobs' scores for every co-follower, and
-- user_apply_score_blend additionally stamps jobs.llm_analysis_id with no
-- job-ownership check.
--
-- Fix: REVOKE EXECUTE from `authenticated` so these are callable only by the
-- backend (service_role). The API already scopes each of these writes to the
-- caller's own active targets in Python (POST /jobs/manual over the user's
-- active_targets; create_analysis's ownership gate) and now issues them on the
-- service-role client — where auth.uid() is NULL and the functions' existing
-- service-role-exempt branch applies. No signature/body change; the follower
-- check is retained (harmless) but moot once direct authenticated execution is
-- revoked. The un-gated poller path was always service-role.

REVOKE EXECUTE ON FUNCTION public.user_upsert_score(jsonb) FROM authenticated;
REVOKE EXECUTE ON FUNCTION public.user_set_scores_included(uuid, uuid[]) FROM authenticated;
REVOKE EXECUTE ON FUNCTION public.user_apply_score_blend(uuid, uuid, integer, uuid) FROM authenticated;
