-- #1153: the ledger spend RPCs read the WHOLE of llm_costs on every call.
--
-- All five filtered the window with `(p_since IS NULL OR created_at >= p_since)`.
-- These functions carry `SET search_path`, so they are never inlined and their
-- statement is planned with p_since as an unknown parameter — and a plan that
-- must also serve `p_since IS NULL` cannot use idx_llm_cost_log_created_at.
-- Measured on prod (PG 17, read-only): total_spend_all_since over a one-day
-- window that matched zero rows ran a parallel seq scan of the full table,
-- ~11k buffers / 320-435 ms, growing with the ledger; the same statement with
-- a plain `created_at >= $1` used the index in 3 buffers / 2 ms. The slowest
-- recorded calls sat at 7.3-7.9 s against the 8 s statement_timeout, and since
-- #1109 these guards fail CLOSED — a killed call refuses LLM work.
--
-- `created_at >= COALESCE(p_since, '-infinity')` is NOT the fix: it makes the
-- all-time (NULL) call walk the index over every row (3.8 s measured vs the
-- 320 ms seq scan). The NULL window is a real caller (insights' all-time
-- views), so each function is now plpgsql, branched on `p_since IS NULL`.
-- plpgsql plans with the call's actual p_since (custom plans, kept while a
-- generic plan would cost more), and the branch keeps each statement free of
-- the NULL case, so even a generic plan of the dated statement can use the
-- index (measured on prod: 3 buffers). The all-time branch keeps the seq scan
-- it needs. Results are unchanged: same
-- predicates otherwise, including the auth.uid() guard (#2 audit) and the
-- user filter.
--
-- Recreated with identical signature/return type/volatility/search_path/owner;
-- CREATE OR REPLACE preserves the existing grants.

CREATE OR REPLACE FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone) RETURNS numeric
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
BEGIN
  IF p_since IS NULL THEN
    RETURN (SELECT COALESCE(SUM(cost_usd), 0)::numeric FROM public.llm_costs);
  END IF;
  RETURN (
    SELECT COALESCE(SUM(cost_usd), 0)::numeric
    FROM   public.llm_costs
    WHERE  created_at >= p_since
  );
END;
$$;
ALTER FUNCTION "public"."total_spend_all_since"("p_since" timestamp with time zone) OWNER TO "postgres";

CREATE OR REPLACE FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) RETURNS numeric
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
BEGIN
  IF p_since IS NULL THEN
    RETURN (
      SELECT COALESCE(SUM(cost_usd), 0)::numeric
      FROM   public.llm_costs
      WHERE  ((p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id)
        -- guard: a JWT caller may only query their own spend; service-role exempt.
        AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
    );
  END IF;
  RETURN (
    SELECT COALESCE(SUM(cost_usd), 0)::numeric
    FROM   public.llm_costs
    WHERE  ((p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id)
      AND  created_at >= p_since
      AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
  );
END;
$$;
ALTER FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";

CREATE OR REPLACE FUNCTION "public"."total_billable_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone, "p_excluded_purposes" "text"[]) RETURNS numeric
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
BEGIN
  IF p_since IS NULL THEN
    RETURN (
      SELECT COALESCE(SUM(cost_usd), 0)::numeric
      FROM   public.llm_costs
      WHERE  ((p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id)
        AND  (p_excluded_purposes IS NULL OR purpose <> ALL (p_excluded_purposes))
        -- guard: a JWT caller may only query their own spend; service-role exempt.
        AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
    );
  END IF;
  RETURN (
    SELECT COALESCE(SUM(cost_usd), 0)::numeric
    FROM   public.llm_costs
    WHERE  ((p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id)
      AND  created_at >= p_since
      AND  (p_excluded_purposes IS NULL OR purpose <> ALL (p_excluded_purposes))
      AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
  );
END;
$$;
ALTER FUNCTION "public"."total_billable_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone, "p_excluded_purposes" "text"[]) OWNER TO "postgres";

CREATE OR REPLACE FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) RETURNS "jsonb"
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
BEGIN
  IF p_since IS NULL THEN
    RETURN (
      SELECT COALESCE(jsonb_object_agg(purpose, total), '{}'::jsonb)
      FROM (
        SELECT purpose, SUM(cost_usd)::numeric AS total
        FROM   public.llm_costs
        WHERE  ((p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id)
          AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
        GROUP BY purpose
      ) g
    );
  END IF;
  RETURN (
    SELECT COALESCE(jsonb_object_agg(purpose, total), '{}'::jsonb)
    FROM (
      SELECT purpose, SUM(cost_usd)::numeric AS total
      FROM   public.llm_costs
      WHERE  ((p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id)
        AND  created_at >= p_since
        AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
      GROUP BY purpose
    ) g
  );
END;
$$;
ALTER FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";

CREATE OR REPLACE FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) RETURNS "jsonb"
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
BEGIN
  IF p_since IS NULL THEN
    RETURN (
      SELECT COALESCE(
               jsonb_object_agg(purpose, jsonb_build_object('sum', total, 'count', n)),
               '{}'::jsonb
             )
      FROM (
        SELECT purpose, SUM(cost_usd)::numeric AS total, COUNT(*)::int AS n
        FROM   public.llm_costs
        WHERE  ((p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id)
          AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
        GROUP BY purpose
      ) g
    );
  END IF;
  RETURN (
    SELECT COALESCE(
             jsonb_object_agg(purpose, jsonb_build_object('sum', total, 'count', n)),
             '{}'::jsonb
           )
    FROM (
      SELECT purpose, SUM(cost_usd)::numeric AS total, COUNT(*)::int AS n
      FROM   public.llm_costs
      WHERE  ((p_user_id IS NULL AND user_id IS NULL) OR user_id = p_user_id)
        AND  created_at >= p_since
        AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
      GROUP BY purpose
    ) g
  );
END;
$$;
ALTER FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";
