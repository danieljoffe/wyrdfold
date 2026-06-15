-- Performance pass from #93: hot-path indexes + a server-side global spend SUM.
-- Only the low-risk, high-value subset is applied here. The N+1 / aggregation
-- CODE rewrites (funnel.py, insights.py GROUP-BY, url_health bulk update) are
-- deferred for human review per #93.

-- ===========================================================================
-- A. Indexes
-- ===========================================================================

-- HIGH (#93 Indexes): Phase-2 daily-cap count was a Seq Scan on every poll
-- cycle. Serves `phase2_quota_remaining` (app/services/fit/daily_cap.py:62-70,
-- also app/services/diagnostics/funnel.py:200): purpose = 'fit.job' AND
-- metadata->>'target_id' = <id> AND created_at >= midnight, with NO user_id.
CREATE INDEX IF NOT EXISTS idx_llm_costs_purpose_target_created
  ON public.llm_costs (purpose, ((metadata ->> 'target_id')), created_at DESC);

-- MEDIUM (#93 Indexes): insights resume scans. Serves the document_type='resume'
-- filter combined with job_posting_id IN (...) (app/services/insights.py:245-251,
-- :585-591); existing indexes cover each column alone, not the combination.
CREATE INDEX IF NOT EXISTS idx_documents_job_doctype
  ON public.documents (job_posting_id, document_type);

-- MEDIUM (#93 Indexes): SMS daily-count filter on sent_at was unindexed. Serves
-- `_sms_count_today` (app/services/notify.py:281-289): user_profile_id +
-- channel='sms' + sent_at >= midnight, on the alert hot path.
CREATE INDEX IF NOT EXISTS idx_notifications_sent_user_channel_sent
  ON public.notifications_sent (user_profile_id, channel, sent_at DESC);

-- ===========================================================================
-- B. Server-side SUM for the global LLM circuit breaker (#93 Functions, #5)
-- ===========================================================================

-- `total_spend_all` (app/services/llm/cost_log.py) selected every llm_costs row
-- since midnight and summed in Python on every poll cycle. This mirrors the
-- existing `total_spend_since` RPC but without the per-user filter: SUM across
-- ALL users since p_since, returned as a single numeric.
CREATE OR REPLACE FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone) RETURNS numeric
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(SUM(cost_usd), 0)::numeric
  FROM   public.llm_costs
  WHERE  (p_since IS NULL OR created_at >= p_since);
$$;

ALTER FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone) OWNER TO "postgres";

GRANT ALL ON FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone) TO "anon";
GRANT ALL ON FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone) TO "authenticated";
GRANT ALL ON FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone) TO "service_role";
