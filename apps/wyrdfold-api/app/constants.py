"""Reserved identity constants for the deployment-modes work (Phase 0).

See ``docs/plan-wyrdfold-deployment-modes.md``. The data model is moving to a
single invariant — every per-user row has a **non-null** owner — so rows created
by the system itself (the cron poller, batch grader, and other api-key callers)
need a real owner rather than the legacy ``user_id IS NULL`` single-tenant marker
that ``get_current_user_id_optional`` threads through the service layer today.

``SYSTEM_USER_ID`` is that owner: a **reserved ``auth.users`` row** with this
fixed id, seeded by migration ``20260701130000_seed_system_principal.sql`` so it
exists identically in every environment (and a fresh self-host bring-up). It is a
real row — not a bare sentinel — so the ``user_id -> auth.users(id) ON DELETE
CASCADE`` foreign key added in Phase 0 step 4 holds for system-owned rows too.
That FK is **defense-in-depth under** the app's #29 erasure flow (which also
purges Storage + external state a DB cascade can't) — a backstop against orphaned
rows, not a replacement. Two properties make the principal safe:

- **It can never authenticate** and **can never collide with a real user.** The
  seed row has no password and no ``identities`` row (no password/OAuth login)
  and a non-routable ``.internal`` email (no magic link). Its id is also not a
  valid v4 UUID (version nibble ``0``, not ``4``), and GoTrue only mints v4s — so
  no human's ``auth.uid()`` will ever equal it, and RLS (``auth.uid() =
  user_id``) keeps every SYSTEM-owned row invisible to every logged-in user.
- **It is attribution, not authentication.** The system writes through the
  service-role client (which bypasses RLS regardless); this id records *which
  principal* authored the row, so "system" is greppable and explicit instead of
  an ambiguous NULL that conflates "system", "none", and "unset".
"""

# Reserved owner for system/cron-authored rows (poller gradings in ``analyses``,
# the ``llm_costs`` ledger, batch output). A real, non-loginable ``auth.users``
# row (see the seed migration + module docstring), so the account-cascade FK
# holds for it too.
SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000001"


def resolve_owner(user_id: str | None) -> str:
    """The owner id to stamp on a row, or filter a read by.

    ``None`` (an api-key / cron caller with no authenticated user) resolves to
    ``SYSTEM_USER_ID`` — the Phase-0 replacement for the legacy ``user_id IS
    NULL`` single-tenant marker. A real user passes through unchanged. Use this
    at every write and per-user read of a table being migrated off the nullable
    ``user_id`` so "the system" is one explicit owner instead of NULL.
    """
    return SYSTEM_USER_ID if user_id is None else user_id
