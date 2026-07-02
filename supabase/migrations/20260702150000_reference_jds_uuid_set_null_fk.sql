-- reference_jds.user_id: text → uuid, nullable, FK ON DELETE SET NULL (#6/#88).
--
-- Settles the last user_id column pending a design call (decision with Daniel,
-- 2026-07-02 — recorded on #6): reference_jds is SHARED-CATALOG content with
-- OPTIONAL attribution, not per-user data. Two facts drive that:
--   * the write path (targets/crud.py) takes user_id: str | None — attribution
--     was optional from the start; NULL means "seeded/unattributed";
--   * the #29 account-deletion flow ANONYMIZES this table (_anonymize_user_id
--     NULLs the link, the shared JD survives) — so NOT NULL would break erasure.
--
-- Hence, unlike the per-user tables (NOT NULL + ON DELETE CASCADE,
-- 20260701160000/20260701170000):
--   * the column stays NULLABLE (NULL = anonymized/unattributed, load-bearing);
--   * the FK is ON DELETE SET NULL — it AUTOMATES the same semantics the #29
--     flow does by hand (user gone → shared JD survives, personal link dropped)
--     and backstops it if the app-layer anonymize step is ever missed.
--
-- Type conversion is free: the table is empty in prod (verified 2026-07-02) and
-- every hypothetical value is app-written from auth.uid(), a uuid. The composite
-- index idx_reference_jds_target_user is rebuilt automatically by ALTER TYPE.
-- No RLS policy references reference_jds.user_id itself (the follower-read
-- policy scopes via target_id/user_targets; its user_targets comparison is
-- handled by 20260702120000).
--
-- Timestamped after 20260702120000 (the text→uuid conversion of the OTHER four
-- tables) so the two apply in either merge order without interaction.

ALTER TABLE public.reference_jds
    ALTER COLUMN user_id TYPE uuid USING user_id::uuid;

ALTER TABLE public.reference_jds
    ADD CONSTRAINT reference_jds_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE SET NULL;
