-- BYOK (#5) — per-user provider API keys, encrypted at rest.
--
-- Keys live in their own table rather than columns on user_profiles so
-- secrets stay out of the row read on every authenticated request, and
-- so adding providers doesn't widen the hot table.
--
-- ciphertext is app-layer AES-256-GCM (nonce || ct || tag) produced by
-- app/services/keys, stored base64-encoded as text — supabase-py speaks
-- PostgREST/JSON, which round-trips base64 text cleanly (raw bytea would
-- come back as a \x-hex string and need per-client decoding). Postgres
-- never sees plaintext. last4 is the only non-secret bit, surfaced in
-- the settings UI ("sk-...a1b2").
--
-- user_id is a bare uuid with no auth.users FK — consistent with every
-- other per-user table in this schema (user_profiles, jobs, scores, …),
-- which enforce the relationship at the app layer via the service-role
-- client. Account-deletion cascade is handled in the app's deletion
-- flow (#29), same as the rest of the per-user data.

CREATE TABLE IF NOT EXISTS "public"."user_api_keys" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "provider" "text" NOT NULL,
    "ciphertext" "text" NOT NULL,
    "last4" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "rotated_at" timestamp with time zone,
    CONSTRAINT "user_api_keys_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "user_api_keys_user_provider_key" UNIQUE ("user_id", "provider"),
    CONSTRAINT "user_api_keys_provider_check" CHECK (
        "provider" = ANY (ARRAY['openrouter'::"text", 'anthropic'::"text", 'voyage'::"text", 'twilio'::"text"])
    )
);

ALTER TABLE "public"."user_api_keys" OWNER TO "postgres";

COMMENT ON TABLE "public"."user_api_keys" IS 'BYOK per-user provider API keys, AES-256-GCM encrypted at rest (#5). Service-role-only.';
COMMENT ON COLUMN "public"."user_api_keys"."ciphertext" IS 'base64(nonce(12) || ciphertext || tag(16)), AES-256-GCM keyed by BYOK_MASTER_KEY.';
COMMENT ON COLUMN "public"."user_api_keys"."last4" IS 'Last 4 chars of the plaintext key for the settings UI. Non-secret.';

-- RLS on, no policies, and grants to service_role ONLY. Unlike the rest
-- of the schema (which grants anon/authenticated and leans on empty RLS),
-- a secrets table withholds those grants entirely — the browser never
-- reaches Postgres directly (BFF → API → service-role), so anon /
-- authenticated have no reason to hold even a blocked grant.
ALTER TABLE "public"."user_api_keys" ENABLE ROW LEVEL SECURITY;

GRANT ALL ON TABLE "public"."user_api_keys" TO "service_role";

CREATE INDEX IF NOT EXISTS "user_api_keys_user_id_idx" ON "public"."user_api_keys" USING "btree" ("user_id");
