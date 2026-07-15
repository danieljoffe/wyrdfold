-- Advisory-lock observability (#350): make "who holds our ingestion locks"
-- queryable through PostgREST so the ingestion-health pass can alarm on a
-- LEAKED lock.
--
-- Background: the poll/discovery advisory locks are SESSION-level, held by
-- whichever pooled PostgREST backend executed try_poll_advisory_lock — not by
-- the API process. When an API dies mid-cycle (kill, OOM, deploy landing
-- mid-poll) the release RPC never runs and the lock stays held by that
-- long-lived backend indefinitely. Every later poll then "skips cleanly" (an
-- INFO log, suppressed on prod), so ingestion silently stops — the exact
-- silent-death class #244/#338 guard one layer up. Reproduced repeatedly by
-- the #57 load-test rig (see #348's "Observed while testing").
--
-- Postgres records no acquisition time for locks, so "age" is not directly
-- measurable; the health check instead pairs "lock currently held" (this
-- function) with behavioral staleness (newest sources.last_polled_at /
-- source_discoveries.discovered_at). A held lock while the stamps are stale
-- means the holder is not doing the work the lock exists to serialize.
--
-- SECURITY DEFINER because pg_stat_activity hides other roles' rows from
-- unprivileged callers and the service role has no direct pg_locks read;
-- read-only over two catalog views, service_role only (same posture as the
-- lock RPCs themselves in 20260623150000_ingestion_resilience.sql).
--
-- Idempotent: CREATE OR REPLACE. Reversible (manual down):
--   DROP FUNCTION IF EXISTS public.advisory_lock_info();

CREATE OR REPLACE FUNCTION "public"."advisory_lock_info"()
    RETURNS TABLE(
        "lock_key" bigint,
        "pid" integer,
        "granted" boolean,
        "backend_start" timestamp with time zone,
        "application_name" "text"
    )
    LANGUAGE "sql"
    SECURITY DEFINER
    SET "search_path" TO 'pg_catalog'
    AS $$
    -- pg_try_advisory_lock(bigint) splits its key across classid (high 32
    -- bits) and objid (low 32 bits); reassemble so callers compare against
    -- the same bigint they locked with.
    SELECT
        ((l.classid::bigint << 32) | l.objid::bigint) AS lock_key,
        l.pid,
        l.granted,
        a.backend_start,
        a.application_name
    FROM pg_locks l
    LEFT JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.locktype = 'advisory';
$$;

ALTER FUNCTION "public"."advisory_lock_info"() OWNER TO "postgres";

COMMENT ON FUNCTION "public"."advisory_lock_info"() IS 'Currently-held session advisory locks with holder backend info, for the ingestion-health leaked-lock alarm (#350). Postgres stores no lock acquisition time; pair with behavioral staleness. service_role only.';

REVOKE ALL ON FUNCTION "public"."advisory_lock_info"() FROM PUBLIC;
REVOKE ALL ON FUNCTION "public"."advisory_lock_info"() FROM "anon", "authenticated";
GRANT EXECUTE ON FUNCTION "public"."advisory_lock_info"() TO "service_role";
