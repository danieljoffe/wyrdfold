-- Phase 0 (deployment-modes): backfill the llm_costs system rows to SYSTEM.
--
-- Paired with the cost_log.py change in the same PR. The system/cron write paths
-- (record / record_embedding / the buffered enqueue) now stamp SYSTEM_USER_ID
-- (app/constants.py) instead of leaving `user_id` NULL, and the per-user reads
-- (list_recent, total_spend, spend_by_purpose) now filter on the SYSTEM id for a
-- caller with no user. This migration re-owns the ~49k legacy rows that were
-- written with `user_id IS NULL` (before this change) so those reads still see
-- them — otherwise system-spend totals would drop the historical rows.
--
-- SYSTEM_USER_ID (00000000-…-0001) is the reserved auth.users row seeded by
-- 20260701130000_seed_system_principal.sql. Idempotent: after this runs there are
-- no NULL-owner rows left, so a re-run updates 0 rows.
--
-- Deploy-window note: the global cost breaker uses `total_spend_all_since`, which
-- sums every row regardless of `user_id`, so it is unaffected throughout. Only
-- the (low-criticality) per-user SYSTEM spend breakdown could momentarily
-- undercount if old code writes a NULL row after this backfill and before the new
-- code deploys — self-correcting as new SYSTEM rows accumulate.

UPDATE public.llm_costs
SET user_id = '00000000-0000-0000-0000-000000000001'
WHERE user_id IS NULL;
