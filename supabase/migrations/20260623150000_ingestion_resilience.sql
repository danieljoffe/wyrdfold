-- Ingestion resilience (#poll-outage): observability + auto-recovery + a
-- single-poll guard for the backend scheduler.
--
-- Background: production ingestion silently died for 10+ days. The only poll
-- trigger was a daily Vercel Hobby cron that broke; every source then hit the
-- consecutive-failure backoff and was disabled the same day; and it was
-- invisible because the failure was only LOGGED (never stored), sources never
-- auto-recovered, and nothing alerted. This migration makes the failure cause
-- queryable in SQL and gives the in-process scheduler a Postgres advisory lock
-- so it can become the robust primary poll trigger without ever double-polling
-- (even alongside the Vercel cron or a second replica).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE FUNCTION. Re-running
-- is a no-op.
--
-- Reversible (manual down — we do not ship a down file):
--   ALTER TABLE public.sources
--     DROP COLUMN IF EXISTS last_error,
--     DROP COLUMN IF EXISTS last_error_at,
--     DROP COLUMN IF EXISTS disabled_at;
--   DROP FUNCTION IF EXISTS public.try_poll_advisory_lock(bigint);
--   DROP FUNCTION IF EXISTS public.release_poll_advisory_lock(bigint);

-- ---------------------------------------------------------------------------
-- 1. Store the failure cause so it's queryable in SQL, not just in logs.
-- ---------------------------------------------------------------------------
ALTER TABLE "public"."sources"
    ADD COLUMN IF NOT EXISTS "last_error" "text",
    ADD COLUMN IF NOT EXISTS "last_error_at" timestamp with time zone,
    ADD COLUMN IF NOT EXISTS "disabled_at" timestamp with time zone;

COMMENT ON COLUMN "public"."sources"."last_error" IS 'Truncated text of the most recent fetch failure for this source. Persisted by the poller so the cause is queryable in SQL (the failure was previously only logged). Cleared on the next successful poll.';
COMMENT ON COLUMN "public"."sources"."last_error_at" IS 'Timestamp of the most recent fetch failure. NULL once the source polls cleanly again.';
COMMENT ON COLUMN "public"."sources"."disabled_at" IS 'When the consecutive-failure backoff auto-disabled this source (set enabled=false). Drives auto-recovery: the poller re-enables sources whose disabled_at is older than SOURCE_RECOVERY_AFTER_HOURS so a transient ATS-wide outage cannot take ingestion down forever. NULL for sources that were never auto-disabled (or that an operator disabled manually after recovery).';

-- Partial index over auto-disabled rows so the recovery sweep is a cheap
-- index scan rather than a full-table filter (most sources are enabled).
CREATE INDEX IF NOT EXISTS "idx_sources_disabled_at"
    ON "public"."sources" USING "btree" ("disabled_at")
    WHERE "disabled_at" IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Advisory-lock RPCs so the scheduler never double-polls.
--
-- pg_try_advisory_lock takes a SESSION-level lock keyed on a bigint. The
-- PostgREST/Supabase client pools connections, so the lock is held for the
-- life of the pooled session that ran the RPC — which is exactly why we MUST
-- pair every successful try_poll_advisory_lock with release_poll_advisory_lock
-- in a finally block (otherwise the lock leaks until that pooled connection is
-- recycled). The scheduler does this.
--
-- A second concurrent poll (another replica, or the Vercel cron firing while
-- the scheduler tick is mid-flight) gets `false` from try_poll_advisory_lock
-- and skips cleanly. Upserts are idempotent regardless, so this is
-- defense-in-depth, not the only correctness guarantee.
--
-- SECURITY DEFINER + granted to service_role ONLY: these are operator/cron
-- primitives, not user-facing. Revoked from anon/authenticated so a leaked
-- user JWT cannot grab or release the ingestion lock.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION "public"."try_poll_advisory_lock"("p_key" bigint)
    RETURNS boolean
    LANGUAGE "sql"
    SECURITY DEFINER
    SET "search_path" TO 'pg_catalog'
    AS $$
    SELECT pg_try_advisory_lock(p_key);
$$;

CREATE OR REPLACE FUNCTION "public"."release_poll_advisory_lock"("p_key" bigint)
    RETURNS boolean
    LANGUAGE "sql"
    SECURITY DEFINER
    SET "search_path" TO 'pg_catalog'
    AS $$
    SELECT pg_advisory_unlock(p_key);
$$;

ALTER FUNCTION "public"."try_poll_advisory_lock"("p_key" bigint) OWNER TO "postgres";
ALTER FUNCTION "public"."release_poll_advisory_lock"("p_key" bigint) OWNER TO "postgres";

COMMENT ON FUNCTION "public"."try_poll_advisory_lock"("p_key" bigint) IS 'Non-blocking session-level advisory lock for the poll cycle. Returns true if acquired, false if another session already holds it. Pair with release_poll_advisory_lock in a finally block — the lock outlives the RPC call on the pooled connection. service_role only.';
COMMENT ON FUNCTION "public"."release_poll_advisory_lock"("p_key" bigint) IS 'Releases the poll advisory lock taken by try_poll_advisory_lock on the same pooled session. Returns false (and warns) if the caller did not hold it. service_role only.';

-- Lock these down: revoke the default PUBLIC EXECUTE and grant only to the
-- service role the API uses for cron/poll work.
REVOKE ALL ON FUNCTION "public"."try_poll_advisory_lock"("p_key" bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION "public"."release_poll_advisory_lock"("p_key" bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION "public"."try_poll_advisory_lock"("p_key" bigint) FROM "anon", "authenticated";
REVOKE ALL ON FUNCTION "public"."release_poll_advisory_lock"("p_key" bigint) FROM "anon", "authenticated";
GRANT EXECUTE ON FUNCTION "public"."try_poll_advisory_lock"("p_key" bigint) TO "service_role";
GRANT EXECUTE ON FUNCTION "public"."release_poll_advisory_lock"("p_key" bigint) TO "service_role";
