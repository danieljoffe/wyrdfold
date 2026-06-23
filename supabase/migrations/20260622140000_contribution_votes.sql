-- Anonymous voting on reference-JD contributions (#5 P3).
--
-- Reference JDs are a shared, collaboratively-built input to a target's
-- scoring profile (#5 P2 attributes each to its contributor). This lets a
-- target's followers down/up-vote a contribution; when the net down-votes
-- reach a quorum the contribution is SUPPRESSED from the merge (the profile is
-- re-merged without it). Votes are ANONYMOUS: RLS only ever exposes a caller
-- their OWN vote, never who else voted — the single surfaced aggregate is
-- reference_jds.suppressed.

CREATE TABLE IF NOT EXISTS "public"."contribution_votes" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "reference_jd_id" "uuid" NOT NULL,
    "user_id" "text" NOT NULL,
    "value" smallint NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "contribution_votes_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "contribution_votes_value_check" CHECK ("value" IN (-1, 1)),
    CONSTRAINT "contribution_votes_reference_jd_id_fkey"
        FOREIGN KEY ("reference_jd_id")
        REFERENCES "public"."reference_jds"("id") ON DELETE CASCADE,
    CONSTRAINT "contribution_votes_user_ref_key"
        UNIQUE ("reference_jd_id", "user_id")
);

-- The suppression outcome lives on the contribution — the only shared signal.
ALTER TABLE "public"."reference_jds"
    ADD COLUMN IF NOT EXISTS "suppressed" boolean NOT NULL DEFAULT false;

-- Tally votes per contribution for the quorum check (service-role read).
CREATE INDEX IF NOT EXISTS "idx_contribution_votes_ref_jd"
    ON "public"."contribution_votes" USING "btree" ("reference_jd_id");

ALTER TABLE "public"."contribution_votes" ENABLE ROW LEVEL SECURITY;

-- Anonymous: a caller can only ever see/write their OWN vote row. No policy
-- exposes another user's vote, so no one can tell who voted how — only the
-- suppression outcome (reference_jds.suppressed) is shared. service_role
-- (the suppression tally + re-merge) bypasses RLS to read all votes.
CREATE POLICY "Users access their own contribution_votes"
    ON "public"."contribution_votes" TO "authenticated"
    USING (((( SELECT "auth"."uid"() AS "uid"))::"text" = "user_id"))
    WITH CHECK (((( SELECT "auth"."uid"() AS "uid"))::"text" = "user_id"));

-- Least privilege: authenticated does row-scoped CRUD (gated by RLS above);
-- service_role keeps full access; anon never touches votes.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "public"."contribution_votes"
    TO "authenticated";
GRANT ALL ON TABLE "public"."contribution_votes" TO "service_role";
