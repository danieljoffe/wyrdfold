-- #60 Pre-scan (embeddings pre-filter), Phase 1: per-job embedding storage.
--
-- The relevance spine embeds each job ONCE (target-INDEPENDENT, cached) so a
-- future pgvector pre-filter can admit only semantically-close jobs to the
-- expensive per-target LLM grade — cost scales with jobs, not jobs×targets.
-- This migration adds ONLY the storage; nothing reads or writes it until the
-- pre-scan code path is flipped on (poller hook gated behind
-- PRESCAN_EMBED_ENABLED, default off — see app/config.py). Merging it triggers
-- no embedding spend and changes no runtime behavior.
--
-- Mirrors ``experience_chunks`` (the existing Voyage + pgvector table): same
-- ``extensions.vector(1024)`` column (voyage-3 is 1024-dim) and the same HNSW
-- ``vector_cosine_ops`` index for cosine nearest-neighbor search.
--
-- Dedicated table (not a column on ``jobs``): the vector is a large, optional,
-- model-versioned artifact with its own lifecycle (re-embed on model change),
-- and keeping it off the hot ``jobs`` row avoids bloating every jobs scan with
-- a 1024-float column the poller/scoring paths never read.
--
-- Cache key: PRIMARY KEY (job_posting_id, model) + a ``content_hash`` column.
-- The writer skips re-embedding when a row for (job, model) already carries the
-- hash of the current (title + cleaned description) — the same content-hash
-- skip the qualification firewall uses via ``jobs.qualified_hash``. ``model``
-- is in the key so a future model swap (e.g. voyage-3 → a successor) can
-- coexist row-by-row during a backfill rather than needing a destructive wipe.
--
-- RLS + grants posture: ENABLE ROW LEVEL SECURITY and the same GRANT triple as
-- ``experience_chunks``. Unlike that table, ``job_embeddings`` is GLOBAL,
-- service-role-written internal data with NO per-user owner column, so it gets
-- NO authenticated/anon SELECT policy — RLS-on with no policy denies anon /
-- authenticated entirely while ``service_role`` (the poller / backfill) bypasses
-- RLS. That is strictly tighter than ``experience_chunks`` and matches how
-- ``jobs`` itself is reached only through SECURITY DEFINER RPCs, never direct
-- client reads.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS and DROP POLICY IF EXISTS
-- before CREATE POLICY — re-running the whole file is a no-op.
--
-- Reversible (manual down — we do not ship a down file):
--   DROP TABLE IF EXISTS public.job_embeddings;  (cascades its index + policy)

-- ---------------------------------------------------------------------------
-- 1. Storage table. ``embedding`` is NOT NULL: a row exists only once we have a
--    vector for it (the writer upserts the full row), so there is no
--    "pending / not-yet-embedded" half-state to represent here — absence of a
--    row is the not-yet-embedded state.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "public"."job_embeddings" (
    "job_posting_id" "uuid" NOT NULL,
    "model" "text" NOT NULL,
    "content_hash" "text" NOT NULL,
    "embedding" "extensions"."vector"(1024) NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "job_embeddings_pkey" PRIMARY KEY ("job_posting_id", "model"),
    CONSTRAINT "job_embeddings_job_posting_id_fkey" FOREIGN KEY ("job_posting_id")
        REFERENCES "public"."jobs"("id") ON DELETE CASCADE
);

ALTER TABLE "public"."job_embeddings" OWNER TO "postgres";

COMMENT ON TABLE "public"."job_embeddings" IS 'Pre-scan (#60): one cached Voyage vector per (job, model). Target-INDEPENDENT, written by the poller/backfill behind PRESCAN_EMBED_ENABLED. service-role-only (RLS denies anon/authenticated).';
COMMENT ON COLUMN "public"."job_embeddings"."model" IS 'Pre-scan (#60): the embedding model ID (e.g. voyage-3). Part of the PK so multiple model versions can coexist during a re-embed.';
COMMENT ON COLUMN "public"."job_embeddings"."content_hash" IS 'Pre-scan (#60): sha256 over the embedded text (title + cleaned description). Lets the writer skip re-embedding unchanged jobs on re-poll.';
COMMENT ON COLUMN "public"."job_embeddings"."embedding" IS 'Pre-scan (#60): the 1024-dim voyage-3 vector. Cosine NN via the HNSW index below.';

-- ---------------------------------------------------------------------------
-- 2. HNSW cosine index — mirrors idx_experience_chunks_embedding. Powers the
--    nearest-neighbor reads (job↔target cosine for the gate; job↔job cosine for
--    the near-dup density measurement). job_embeddings is NOT a hot,
--    continuously-written table (writes are the once-per-job embed, not the
--    per-poll job upserts), so a plain CREATE INDEX is fine — no
--    ``index-lock-ok`` marker needed (see tests/test_migration_safety.py;
--    the build also runs over an empty table at apply time).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS "idx_job_embeddings_embedding"
    ON "public"."job_embeddings"
    USING "hnsw" ("embedding" "extensions"."vector_cosine_ops");

-- ---------------------------------------------------------------------------
-- 3. RLS + grants. GRANT triple mirrors experience_chunks; RLS is enabled with
--    NO policy, so anon / authenticated are denied and only service_role (which
--    bypasses RLS) can read/write. See the header for why this table has no
--    per-user ownership policy.
-- ---------------------------------------------------------------------------
ALTER TABLE "public"."job_embeddings" ENABLE ROW LEVEL SECURITY;

GRANT ALL ON TABLE "public"."job_embeddings" TO "anon";
GRANT ALL ON TABLE "public"."job_embeddings" TO "authenticated";
GRANT ALL ON TABLE "public"."job_embeddings" TO "service_role";
