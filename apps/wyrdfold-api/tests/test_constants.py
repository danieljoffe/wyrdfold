"""Guards on the reserved identity constants (Phase 0, deployment-modes)."""

from __future__ import annotations

import uuid

from app.constants import SYSTEM_USER_ID, resolve_owner


def test_system_user_id_is_a_well_formed_uuid() -> None:
    assert str(uuid.UUID(SYSTEM_USER_ID)) == SYSTEM_USER_ID


def test_system_user_id_cannot_collide_with_a_real_gotrue_user() -> None:
    """The whole point of the sentinel: GoTrue mints v4 UUIDs, so a NON-v4
    reserved id can never equal a real user's ``auth.uid()``. That is what keeps
    SYSTEM-owned rows invisible to every user under ``auth.uid() = user_id`` RLS.
    If someone ever "tidies" this into a v4 UUID, that invariant silently breaks —
    hence this test.
    """
    assert uuid.UUID(SYSTEM_USER_ID).version != 4


def test_system_user_id_is_stable() -> None:
    # Pinned: the value is referenced by migrations (backfill) and the cron write
    # paths; it must not drift. Change here == a coordinated data migration.
    assert SYSTEM_USER_ID == "00000000-0000-0000-0000-000000000001"


def test_resolve_owner_maps_none_to_system() -> None:
    # A caller with no user (api-key / cron) → the system principal.
    assert resolve_owner(None) == SYSTEM_USER_ID


def test_resolve_owner_passes_a_real_user_through() -> None:
    assert resolve_owner("11111111-1111-4111-8111-111111111111") == (
        "11111111-1111-4111-8111-111111111111"
    )
