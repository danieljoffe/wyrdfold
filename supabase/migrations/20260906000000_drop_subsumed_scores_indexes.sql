-- #698 (R3 §4, re-planned): drop the six scores indexes no live path needs.
--
-- scores carried 12 indexes / 228 MB. The 2026-09-04 measurement pass
-- (issue comment has the full plans) EXPLAINed every reachable query shape
-- — the two list RPCs, the #996 candidate windows, the phase-2 grade
-- queue, poller row lookups, insights/funnel/export/notify/url-health
-- entries — against prod, and confirmed with a drop-them counterfactual on
-- a 480k-row replica carrying all 12 definitions. Findings, per index:
--
--   idx_scores_target_excl_score_jpid (61 MB) — built for get_target_jobs'
--     score keyset, which the app bypasses unconditionally
--     (_RpcIneligibleError on sort=score / min_score>0; score sort routes
--     through get_cross_target_jobs, which is index-only on
--     idx_scores_live_dedup). Appears in ZERO live prod plans.
--   idx_scores_target_excl_recency_jpid (49 MB) — same bypassed keyset,
--     recency flavor. Its one plausible ordered consumer, the phase-2
--     queue, orders DESC **NULLS LAST**; a plain DESC index is NULLS FIRST
--     and cannot serve that order (the #996 lesson), so it was never more
--     than an oversized entry filter.
--   idx_jts_job (7.9 MB) — (job_posting_id): strict prefix of the unique
--     key (job_posting_id, target_id), which serves every jpid lookup.
--   idx_jts_target (9.4 MB) — (target_id): strict prefix of
--     idx_jts_target_score, the retained entry index.
--   scores_target_recency_idx (9.3 MB) — only ever chosen as a plain
--     target-entry bitmap (funnel, learning projection); NULLS FIRST, so
--     never an ordered scan for the queue. Interchangeable with the
--     retained entry index.
--   idx_jts_scoring_status (6.7 MB) — partial on <>'complete' = MOST of
--     the table (275k rows). Its only equality consumer is the phase-2
--     queue, where prod chose a BitmapAnd through it that made the query
--     8x SLOWER than the plan without it (188ms -> 24ms measured).
--
-- Keep-set (6): the two constraint indexes (unique key + pkey),
-- idx_scores_live_dedup (cross-target RPC, index-only), idx_jts_target_score
-- (the one general (target_id, excluded, ...) entry — also covers FK-side
-- scans for target deletion via its prefix), and the two #996 window
-- indexes. Counterfactual: all ten measured shapes stay milliseconds-scale
-- on the keep-set; the poller's write path maintains half as many indexes.
--
-- Prod protocol: dropped by hand as DROP INDEX CONCURRENTLY at release
-- (plain DROP INDEX takes ACCESS EXCLUSIVE — brief, but it queues behind
-- any running query and everything queues behind it; scores is
-- continuously read and written). This file then stamps as a no-op via
-- IF EXISTS, and gives local/CI/fresh schemas the same end state.
-- Recreation, if a regression ever surfaces, is one CREATE INDEX
-- CONCURRENTLY away — definitions preserved above and in git history.

DROP INDEX IF EXISTS public.idx_scores_target_excl_score_jpid;
DROP INDEX IF EXISTS public.idx_scores_target_excl_recency_jpid;
DROP INDEX IF EXISTS public.idx_jts_job;
DROP INDEX IF EXISTS public.idx_jts_target;
DROP INDEX IF EXISTS public.scores_target_recency_idx;
DROP INDEX IF EXISTS public.idx_jts_scoring_status;
