-- perf(#365 follow-up): make the cross-target dedup an INDEX-ONLY scan.
--
-- get_cross_target_jobs dedups the user's whole cross-target scored set (~19k
-- rows for a heavy user) then returns a 6-row page. EXPLAIN showed the dominant
-- cold cost was the Bitmap Heap Scan on scores (~4,441 heap pages) fetching
-- job_posting_id / scoring_status / axis_scores per row — plus a full live-jobs
-- hash scan. Cold (buffers evicted by poller churn on the small instance) that
-- was ~7.6s server-side; warm ~300ms.
--
-- This rebuilds the existing idx_scores_target_excl_score_jpid — same key
-- (target_id, excluded, score DESC, job_posting_id DESC) — with scoring_status
-- + axis_scores added as INCLUDE columns, so the dedup CTE (rewritten in the
-- next migration to drop its jobs join) reads ONLY the index, never the scores
-- heap. Same key ⇒ no other query regresses; net index count unchanged.
--
-- index-lock-ok: rebuilds one existing index on scores (~30k rows, ~1-2s build).
-- CONCURRENTLY can't run in the txn `supabase db push` wraps each file in (#112);
-- a brief write lock is acceptable at current scale, and this is a poll-quiet
-- deploy window. The poller retries transient write failures.

DROP INDEX IF EXISTS public.idx_scores_target_excl_score_jpid;

CREATE INDEX idx_scores_target_excl_score_jpid ON public.scores
    (target_id, excluded, score DESC, job_posting_id DESC)
    INCLUDE (scoring_status, axis_scores);
