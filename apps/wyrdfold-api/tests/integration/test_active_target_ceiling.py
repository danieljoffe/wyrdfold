"""SEC-M1 (audit 2026-07-18): an absolute DB ceiling caps active targets per
user (25), independent of the Python per-tier cap, so a direct PostgREST
`{is_active:true}` spray can't enrol an unbounded number of poller-billed
targets. Uses an AFTER-ROW trigger so a single BULK insert can't slip past.

Requires migration 20260718120000. Runs against the local Supabase stack.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from supabase import Client

pytestmark = pytest.mark.integration

_CEILING = 25


def _active_count(service_client: Client, uid: str) -> int:
    resp = (
        service_client.table("user_targets")
        .select("id", count="exact", head=True)  # type: ignore[arg-type]
        .eq("user_id", uid)
        .eq("is_active", True)
        .execute()
    )
    return resp.count or 0


def test_active_target_ceiling(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> None:
    uid_a, uid_b = two_seeded_users
    tids: list[str] = []
    try:
        rows = cast(
            list[dict[str, Any]],
            service_client.table("targets")
            .insert([{"label": f"Ceiling {uid_a[:6]} {i}"} for i in range(_CEILING + 1)])
            .execute()
            .data
            or [],
        )
        tids = [r["id"] for r in rows]

        # User A: exactly the ceiling is allowed...
        service_client.table("user_targets").insert(
            [{"user_id": uid_a, "target_id": t, "is_active": True} for t in tids[:_CEILING]]
        ).execute()
        assert _active_count(service_client, uid_a) == _CEILING

        # ...the next one over is rejected.
        with pytest.raises(Exception) as e:
            service_client.table("user_targets").insert(
                {"user_id": uid_a, "target_id": tids[_CEILING], "is_active": True}
            ).execute()
        assert "ceiling" in str(e.value).lower() or "23514" in str(e.value), e.value
        assert _active_count(service_client, uid_a) == _CEILING

        # Bulk-safety: user B tries to slip past in ONE statement (ceiling+1
        # rows). The AFTER-ROW trigger fires on the crossing row and rolls back
        # the whole statement — B ends with zero, not ceiling+1.
        with pytest.raises(Exception):
            service_client.table("user_targets").insert(
                [{"user_id": uid_b, "target_id": t, "is_active": True} for t in tids]
            ).execute()
        assert _active_count(service_client, uid_b) == 0
    finally:
        if tids:
            service_client.table("targets").delete().in_("id", tids).execute()
