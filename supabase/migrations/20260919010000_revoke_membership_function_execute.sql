-- #1083: only the API may call the membership functions.
--
-- In plain terms: the three functions that create or activate a user's target
-- memberships (create_target_and_link, activate_user_target,
-- swap_user_target_active) could be called by anyone holding a signed-in
-- user's own login token, because Supabase grants EXECUTE on every new
-- function to the anon and authenticated roles by default. The plan's
-- active-target cap is a number the API passes in, so a user calling
-- activate_user_target directly could pass any cap they liked and hold more
-- active targets than their plan allows (the release-gate check for #1071 did
-- exactly that on staging: p_active_limit=999, two active on a cap of one).
-- They could only ever touch their OWN memberships (row-level security on
-- user_targets scopes every write to auth.uid()) and could not mint catalog
-- targets (targets has no insert policy for authenticated), so this is a
-- plan-cap bypass, not a data leak. The API's service role is the only
-- intended caller; after this migration it is the only role that can call
-- them. A direct call from any other role is refused with SQLSTATE 42501
-- ("permission denied for function ...") before the function body runs, so a
-- refused call writes nothing.
--
-- Why both PUBLIC and the two named roles: the default ACL carries the
-- Postgres-wide PUBLIC grant (`=X`) AND explicit `anon=X` / `authenticated=X`
-- entries from Supabase's default privileges, so revoking from PUBLIC alone
-- would leave the named grants in place.
--
-- The functions are SECURITY INVOKER, so nothing changes for the service
-- role, which bypasses row-level security anyway. swap_user_target_active
-- calls activate_user_target internally; that inner call runs as the same
-- (service) role and keeps working. The GRANTs below are redundant with the
-- default ACL today and exist so the intended caller is stated, not implied.
--
-- Durable note for later migrations: CREATE OR REPLACE FUNCTION keeps these
-- privileges, but DROP FUNCTION + CREATE re-applies Supabase's default
-- privileges and hands EXECUTE back to anon and authenticated. Any future
-- migration that drops and recreates one of these must repeat the REVOKE.
--
-- The table-level rule (whether a user's own token may flip
-- user_targets.is_active directly, bounded only by the hard ceiling of 25 in
-- trg_enforce_active_target_ceiling) is deliberately NOT changed here; that
-- decision is tracked on #1083.

REVOKE EXECUTE ON FUNCTION public.create_target_and_link(uuid, text, text, text, text, jsonb, jsonb, boolean, integer)
    FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.activate_user_target(uuid, uuid, integer, integer, text, uuid)
    FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.swap_user_target_active(uuid, uuid, uuid, integer, integer, text, uuid)
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.create_target_and_link(uuid, text, text, text, text, jsonb, jsonb, boolean, integer)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.activate_user_target(uuid, uuid, integer, integer, text, uuid)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.swap_user_target_active(uuid, uuid, uuid, integer, integer, text, uuid)
    TO service_role;

NOTIFY pgrst, 'reload schema';
