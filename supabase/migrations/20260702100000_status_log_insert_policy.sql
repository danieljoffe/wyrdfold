-- #88 Phase 2: let authenticated users insert their OWN status_log rows.
--
-- status_log already has the per-user SELECT policy
-- (20260616020000_status_log_user_attribution.sql); writes stayed
-- service-role because no INSERT policy existed. The status-update endpoint
-- migrates onto the RLS-bound user client, so the insert now arrives as
-- `authenticated` and needs a WITH CHECK policy pinning user_id to the
-- caller.
--
-- Unlike llm_costs (whose ledger a user must never write — forged rows would
-- bypass budget enforcement), status_log is the user's own pipeline history:
-- a user forging their own audit rows affects nothing but their own display.
-- No UPDATE/DELETE policy — history rows are append-only for authenticated.
create policy "Users insert their own status_log" on public.status_log
  for insert to authenticated
  with check ((select auth.uid()) = user_id);
