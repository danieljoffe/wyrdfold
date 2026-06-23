-- Public waitlist for non-invited visitors on the marketing homepage.
--
-- The landing page is publicly indexed; the majority of visitors are NOT
-- invited to the private beta. The primary CTA captures their email so they
-- can be invited later. Signups are written ONLY by the server-side BFF route
-- (`/api/waitlist`) using the service-role key, which bypasses RLS.
--
-- SECURITY POSTURE (audit #29): this table holds PII (emails) and must never
-- be readable, writable, or enumerable by anon or authenticated browser
-- sessions. RLS is enabled with NO policy for those roles, so every
-- anon/authenticated SELECT/INSERT/UPDATE/DELETE is denied by default. Only
-- service_role (the BFF) can touch it. The email is UNIQUE so the BFF can
-- treat duplicate signups idempotently WITHOUT leaking existence (it ON
-- CONFLICT DO NOTHING and always returns a generic success — no enumeration).

CREATE TABLE IF NOT EXISTS "public"."waitlist_signups" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "email" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "waitlist_signups_pkey" PRIMARY KEY ("id"),
    -- Case-insensitive uniqueness is enforced by the index below; this plain
    -- UNIQUE keeps the exact stored value unique too (defense in depth).
    CONSTRAINT "waitlist_signups_email_key" UNIQUE ("email"),
    -- Cheap sanity bound; the BFF does the real RFC-ish validation + length
    -- cap before insert. Stops absurd payloads even if the BFF is bypassed.
    CONSTRAINT "waitlist_signups_email_len_check"
        CHECK ("char_length"("email") BETWEEN 3 AND 320)
);

-- The BFF normalises to lower-case before insert; this unique index makes the
-- de-dupe case-insensitive regardless, so "A@x.com" and "a@x.com" collapse to
-- one row (and one generic-success response).
CREATE UNIQUE INDEX IF NOT EXISTS "idx_waitlist_signups_email_lower"
    ON "public"."waitlist_signups" USING "btree" ("lower"("email"));

ALTER TABLE "public"."waitlist_signups" ENABLE ROW LEVEL SECURITY;

-- Deliberately NO policy for anon/authenticated: RLS-enabled + no policy =
-- deny all for those roles. service_role bypasses RLS, so the BFF can write.

-- Least privilege: service_role does everything (the only legitimate caller).
-- anon/authenticated get NOTHING — not even SELECT — so the table is neither
-- readable nor enumerable from the browser. Revokes are explicit in case a
-- broad default grant exists from a prior `GRANT ... ON ALL TABLES`.
REVOKE ALL ON TABLE "public"."waitlist_signups" FROM "anon";
REVOKE ALL ON TABLE "public"."waitlist_signups" FROM "authenticated";
GRANT ALL ON TABLE "public"."waitlist_signups" TO "service_role";
