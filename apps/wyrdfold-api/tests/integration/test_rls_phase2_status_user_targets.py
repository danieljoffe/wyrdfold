"""RLS gate for #88 Phase 2 — status writes + user_targets prefs.

Proves the tables behind the newly migrated endpoints enforce per-user
scoping at Postgres, through the JWT-bound user client:

* ``user_jobs``       — full CRUD self-policy: own upsert works, forging
                        another user's row is denied.
* ``status_log``      — self-INSERT policy (20260702100000): own audit row
                        inserts, a row attributed to another user is denied;
                        SELECT stays scoped by the existing read policy.
* ``user_targets``    — self-ALL policy: own prefs update works, another
                        user's row is invisible to UPDATE (0 rows touched,
                        no error — the crud helpers surface that as None/404).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from postgrest.exceptions import APIError
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_posting(service_client: Client) -> Iterator[str]:
    """A shared-catalog posting for user_jobs/status_log rows to hang off."""
    source_id = str(uuid.uuid4())
    posting_id = str(uuid.uuid4())
    board_token = f"test-{uuid.uuid4().hex[:12]}"
    try:
        service_client.table("sources").insert(
            {
                "id": source_id,
                "board_token": board_token,
                "company_name": "Acme",
                "provider": "greenhouse",
            }
        ).execute()
        service_client.table("jobs").insert(
            {
                "id": posting_id,
                "external_id": "ext-p2",
                "source_id": source_id,
                "title": "Phase 2 Job",
                "company_name": "Acme",
            }
        ).execute()
        yield posting_id
    finally:
        # source delete cascades to jobs → user_jobs/status_log rows.
        service_client.table("sources").delete().eq("id", source_id).execute()


# ---- user_jobs -------------------------------------------------------------


def test_user_can_upsert_own_user_job(
    seeded_posting: str,
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, _ = two_seeded_users
    client_a = user_client_factory(uid_a)

    client_a.table("user_jobs").upsert(
        {"user_id": uid_a, "job_posting_id": seeded_posting, "status": "applied"},
        on_conflict="user_id,job_posting_id",
    ).execute()

    rows = (
        client_a.table("user_jobs")
        .select("status")
        .eq("job_posting_id", seeded_posting)
        .execute()
        .data
    )
    assert [r["status"] for r in rows] == ["applied"]


def test_user_cannot_forge_user_job_for_other_user(
    seeded_posting: str,
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    with pytest.raises(APIError):
        client_a.table("user_jobs").insert(
            {"user_id": uid_b, "job_posting_id": seeded_posting, "status": "rejected"}
        ).execute()


# ---- status_log ------------------------------------------------------------


def test_user_can_insert_own_status_log_row(
    seeded_posting: str,
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    """The 20260702100000 INSERT policy: a user appends their own history."""
    uid_a, _ = two_seeded_users
    client_a = user_client_factory(uid_a)

    client_a.table("status_log").insert(
        {
            "posting_id": seeded_posting,
            "old_status": "new",
            "new_status": "applied",
            "user_id": uid_a,
        }
    ).execute()

    rows = (
        client_a.table("status_log")
        .select("new_status")
        .eq("posting_id", seeded_posting)
        .execute()
        .data
    )
    assert [r["new_status"] for r in rows] == ["applied"]


def test_user_cannot_insert_status_log_for_other_user(
    seeded_posting: str,
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    """WITH CHECK pins user_id to the caller — forged attribution is refused."""
    uid_a, uid_b = two_seeded_users
    client_a = user_client_factory(uid_a)

    with pytest.raises(APIError):
        client_a.table("status_log").insert(
            {
                "posting_id": seeded_posting,
                "old_status": "new",
                "new_status": "ghosted",
                "user_id": uid_b,
            }
        ).execute()


def test_status_log_select_scoped_to_caller(
    seeded_posting: str,
    two_seeded_users: tuple[str, str],
    service_client: Client,
    user_client_factory: Callable[[str], Client],
) -> None:
    """Two users act on the same shared posting; each sees only their own
    transitions — the exact scenario behind the #113 history fix."""
    uid_a, uid_b = two_seeded_users
    service_client.table("status_log").insert(
        [
            {"posting_id": seeded_posting, "old_status": "new", "new_status": "applied", "user_id": uid_a},
            {"posting_id": seeded_posting, "old_status": "new", "new_status": "rejected", "user_id": uid_b},
        ]
    ).execute()
    client_a = user_client_factory(uid_a)

    # No .eq("user_id", ...) — RLS alone must scope this.
    rows = (
        client_a.table("status_log")
        .select("user_id, new_status")
        .eq("posting_id", seeded_posting)
        .execute()
        .data
    )

    seen = {r["user_id"] for r in rows}
    assert seen == {uid_a}, "RLS leak: user A sees user B's status history"


# ---- user_targets ----------------------------------------------------------


@pytest.fixture
def seeded_user_targets(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str, str]]:
    """One shared target linked to both users. Yields (uid_a, uid_b, target_id)."""
    uid_a, uid_b = two_seeded_users
    target_id = str(uuid.uuid4())
    try:
        service_client.table("targets").insert(
            {"id": target_id, "label": "P2 Shared Target"}
        ).execute()
        service_client.table("user_targets").insert(
            [
                {"user_id": uid_a, "target_id": target_id},
                {"user_id": uid_b, "target_id": target_id},
            ]
        ).execute()
        yield uid_a, uid_b, target_id
    finally:
        service_client.table("user_targets").delete().eq("target_id", target_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def test_user_can_update_own_user_target_prefs(
    seeded_user_targets: tuple[str, str, str],
    user_client_factory: Callable[[str], Client],
) -> None:
    uid_a, _, target_id = seeded_user_targets
    client_a = user_client_factory(uid_a)

    # pref_remote_ok defaults to TRUE (20260624100000) — write False so the
    # probe value is distinguishable from an untouched row.
    resp = (
        client_a.table("user_targets")
        .update({"pref_remote_ok": False})
        .eq("user_id", uid_a)
        .eq("target_id", target_id)
        .execute()
    )
    assert len(resp.data) == 1
    assert resp.data[0]["pref_remote_ok"] is False


def test_user_update_cannot_touch_other_users_prefs(
    seeded_user_targets: tuple[str, str, str],
    service_client: Client,
    user_client_factory: Callable[[str], Client],
) -> None:
    """An UPDATE aimed at B's row through A's client touches 0 rows — RLS
    filters it out before the write. This is the backstop behind the crud
    helpers' 404-on-None contract."""
    uid_a, uid_b, target_id = seeded_user_targets
    client_a = user_client_factory(uid_a)

    # Attack with False — pref_remote_ok defaults to TRUE, so an untouched
    # victim row stays True and a mutated one is unambiguous.
    resp = (
        client_a.table("user_targets")
        .update({"pref_remote_ok": False})
        .eq("user_id", uid_b)
        .eq("target_id", target_id)
        .execute()
    )
    assert resp.data == [], "RLS leak: user A updated user B's user_targets row"

    b_row = (
        service_client.table("user_targets")
        .select("pref_remote_ok")
        .eq("user_id", uid_b)
        .eq("target_id", target_id)
        .execute()
        .data
    )
    assert b_row[0]["pref_remote_ok"] is True, "B's prefs were mutated"
