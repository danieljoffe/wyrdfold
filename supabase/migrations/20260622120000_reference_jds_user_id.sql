-- Attribute each reference JD to the user who added it (#5 refinement layer).
--
-- The shared targets.scoring_profile is merged from a target's reference JDs.
-- Without contributor attribution, one prolific user who adds five JDs skews
-- the shared rubric five times as hard as five users who each add one. Adding
-- user_id lets the merge de-bias by contributor: collapse each contributor's
-- JDs into one per-contributor profile, then merge those equally.
--
-- Nullable + text (no FK), matching public.user_targets.user_id: legacy rows
-- and operator/system-seeded JDs carry NULL and merge as a single "system"
-- contributor. reference_jds stays a shared, authenticated-readable catalog
-- (RLS unchanged) — user_id is attribution data the service-role writer sets
-- from the caller's JWT, plus the key the "remove your own contribution" path
-- scopes deletes by; it is not an RLS scoping column.
ALTER TABLE "public"."reference_jds"
    ADD COLUMN IF NOT EXISTS "user_id" "text";

-- The de-bias merge groups a target's reference JDs by contributor; the
-- remove-own delete filters by (target_id, user_id). Composite covers both.
CREATE INDEX IF NOT EXISTS "idx_reference_jds_target_user"
    ON "public"."reference_jds" USING "btree" ("target_id", "user_id");
