-- #2 audit (security hardening): guard the per-user spend RPCs + trim inert grants.
--
-- Part A — in-body auth.uid() guard on the per-user spend RPCs.
--   total_spend_since / spend_by_purpose_since / cost_by_purpose_since are
--   SECURITY INVOKER and take a caller-supplied p_user_id with NO in-body
--   identity check. They are safe TODAY only because RLS on llm_costs scopes a
--   user-JWT caller to their own rows -- which makes them one `SECURITY DEFINER`
--   edit away from a P1 cross-user financial-data leak (a DEFINER body bypasses
--   RLS and would faithfully return ANY user's spend for ANY p_user_id). Add a
--   defence-in-depth guard that is independent of RLS and of the
--   DEFINER/INVOKER toggle: a JWT caller may only ask for their own spend;
--   service-role (auth.uid() IS NULL) stays unrestricted so the API + poller
--   keep working. Recreated INVOKER (LANGUAGE sql STABLE), identical
--   signature/search_path/owner; CREATE OR REPLACE preserves existing grants.
--   Mirrors the hardening intent of #111 / #23.

CREATE OR REPLACE FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) RETURNS numeric
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(SUM(cost_usd), 0)::numeric
  FROM   public.llm_costs
  WHERE  (
           (p_user_id IS NULL AND user_id IS NULL)
           OR user_id = p_user_id
         )
    AND  (p_since IS NULL OR created_at >= p_since)
    -- guard: a JWT caller may only query their own spend; service-role exempt.
    AND  (auth.uid() IS NULL OR p_user_id = auth.uid());
$$;
ALTER FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";

CREATE OR REPLACE FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(jsonb_object_agg(purpose, total), '{}'::jsonb)
  FROM (
    SELECT purpose,
           SUM(cost_usd)::numeric AS total
    FROM   public.llm_costs
    WHERE  (
             (p_user_id IS NULL AND user_id IS NULL)
             OR user_id = p_user_id
           )
      AND  (p_since IS NULL OR created_at >= p_since)
      AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
    GROUP BY purpose
  ) g;
$$;
ALTER FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";

CREATE OR REPLACE FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(
           jsonb_object_agg(purpose, jsonb_build_object('sum', total, 'count', n)),
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
      AND  (auth.uid() IS NULL OR p_user_id = auth.uid())
    GROUP BY purpose
  ) g;
$$;
ALTER FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";

-- Part B — trim the inert anon grant on the three per-user spend RPCs.
--   No anonymous caller ever legitimately asks for a user's spend; the per-
--   request user client authenticates as `authenticated` (its JWT), not `anon`.
--   Revoking anon also closes the one hole the Part-A guard cannot: an anon
--   caller has auth.uid() = NULL, so under a hypothetical DEFINER flip the guard
--   would exempt it. authenticated + service_role keep EXECUTE. REVOKE is
--   idempotent.
REVOKE ALL ON FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) FROM "anon";
REVOKE ALL ON FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) FROM "anon";
REVOKE ALL ON FUNCTION "public"."cost_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) FROM "anon";

-- Part C — notifications_sent is a service-role-only alert-dedup ledger.
--   It is RLS-on with ZERO policy (so anon/authenticated access is denied
--   today), but it still carries the baseline GRANT ALL to anon/authenticated
--   -- the same inert-but-pointless attack surface #111 revoked for
--   sources / source_discoveries / target_derive_jd_cache / wyrdfold_beta_invites.
--   If a permissive policy or a DISABLE ROW LEVEL SECURITY is ever added here,
--   that standing grant would immediately expose who-was-alerted-about-what.
--   The app only ever writes it via the service-role client. service_role keeps
--   ALL. Idempotent.
REVOKE ALL ON TABLE "public"."notifications_sent" FROM "anon", "authenticated";
