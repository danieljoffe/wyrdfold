"""Reserved identity constants for the deployment-modes work (Phase 0).

See ``docs/plan-wyrdfold-deployment-modes.md``. The data model is moving to a
single invariant — every per-user row has a **non-null** owner — so rows created
by the system itself (the cron poller, batch grader, and other api-key callers)
need a real owner rather than the legacy ``user_id IS NULL`` single-tenant marker
that ``get_current_user_id_optional`` threads through the service layer today.

``SYSTEM_USER_ID`` is that owner: a reserved sentinel the system stamps onto its
own rows. It is deliberately **not** a row in ``auth.users`` and carries **no**
``auth.users`` foreign key — consistent with every other per-user table in this
schema, which enforce the user relationship at the app layer and cascade account
deletion through the app's erasure flow (#29), not a DB constraint (see the BYOK
migration ``20260614000000_byok_user_api_keys.sql``). Two properties make the
sentinel safe:

- **It is not a valid v4 UUID** — the version nibble is ``0``, not ``4``. GoTrue
  mints v4 UUIDs, so it can never issue a real user with this id; no human's
  ``auth.uid()`` will ever equal it, and RLS (``auth.uid() = user_id``) therefore
  keeps every SYSTEM-owned row invisible to every logged-in user.
- **It is attribution, not authentication.** The system writes through the
  service-role client (which bypasses RLS regardless); this id only records
  *which principal* authored the row, so "system" is greppable and explicit
  instead of an ambiguous NULL that conflates "system", "none", and "unset".
"""

# Reserved owner for system/cron-authored rows (poller gradings in ``analyses``,
# the ``llm_costs`` ledger, batch output). NOT a real auth user — see the module
# docstring for why a sentinel (and not an ``auth.users`` row) is the right model.
SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000001"
