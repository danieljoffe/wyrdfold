-- #105 perf: per-purpose LLM cost aggregation server-side.
--
-- compute_skills_cost (app/services/insights.py) fetched every llm_costs row
-- (purpose, cost_usd) for the user and folded them into a per-purpose
-- {total_cost, call_count} in Python. This RPC does that fold in one GROUP BY
-- pass and returns it as jsonb, collapsing N rows of transfer to one small
-- object.
--
-- Mirrors spend_by_purpose_since: LANGUAGE sql STABLE (SECURITY INVOKER, so a
-- user-JWT client is still RLS-scoped), fixed search_path, owned by postgres,
-- granted to anon/authenticated/service_role, same
-- (p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id scoping.
--
-- BYTE-IDENTITY CONTRACT (must match the Python it replaces): returns RAW
-- SUM(cost_usd)::numeric + COUNT(*)::int per purpose. The caller rounds the
-- sum in Python — Postgres round() is half-away-from-zero while Python's is
-- banker's (half-to-even), so rounding in SQL would diverge. call_count is an
-- exact integer count, no rounding concern. (purpose is NOT NULL, so no
-- NULL-key risk in jsonb_object_agg.)
CREATE OR REPLACE FUNCTION "public"."cost_by_purpose_since"(
    "p_user_id" "uuid",
    "p_since" timestamp with time zone
) RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(
           jsonb_object_agg(
             purpose,
             jsonb_build_object('sum', total, 'count', n)
           ),
           '{}'::jsonb
         )
  FROM (
    SELECT purpose,
           SUM(cost_usd)::numeric AS total,
           COUNT(*)::int          AS n
    FROM   public.llm_costs
    WHERE  (
             (p_user_id IS NULL AND user_id IS NULL)
             OR user_id = p_user_id
           )
      AND  (p_since IS NULL OR created_at >= p_since)
    GROUP BY purpose
  ) g;
$$;

ALTER FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";

GRANT ALL ON FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "anon";
GRANT ALL ON FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "authenticated";
GRANT ALL ON FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "service_role";
