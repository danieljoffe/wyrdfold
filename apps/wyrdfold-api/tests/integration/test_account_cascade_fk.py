"""Account-cascade FK proof for the text->uuid converted tables (#6/#88 tail).

20260702120000 converted ``user_id`` on user_targets / job_feedback /
contribution_votes / target_learning_log from text to uuid and added
``FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE``.
These tests prove the guard both ways, per table:

* an orphan insert (a well-formed uuid that is NOT an auth user) is
  REJECTED with Postgres FK violation 23503 — the negative case that
  would silently pass if the constraint regressed; and
* deleting the auth user cascades, so no per-user row outlives its owner.

Runs on the service-role client on purpose: RLS is bypassed, so the only
thing standing between an orphan row and the table is the FK itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from postgrest.exceptions import APIError
from supabase import Client

from tests.integration.conftest import create_auth_user, delete_auth_user

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_parents(service_client: Client) -> Iterator[dict[str, str]]:
    """Catalog parents the four tables' other FKs need: a source, a job, a
    target, and a reference JD. Cleanup cascades from source/target."""
    source_id = (
        service_client.table("sources")
        .insert({"board_token": "fk-int", "company_name": "FK Co"})
        .execute()
        .data[0]["id"]
    )
    target_id = (
        service_client.table("targets")
        .insert({"label": "FK Int Target"})
        .execute()
        .data[0]["id"]
    )
    job_id = (
        service_client.table("jobs")
        .insert(
            {
                "external_id": "fk-int-1",
                "source_id": source_id,
                "title": "Engineer",
                "company_name": "FK Co",
            }
        )
        .execute()
        .data[0]["id"]
    )
    ref_id = (
        service_client.table("reference_jds")
        .insert({"target_id": target_id, "jd_text": "FK proof JD"})
        .execute()
        .data[0]["id"]
    )
    try:
        yield {
            "source_id": source_id,
            "target_id": target_id,
            "job_id": job_id,
            "ref_id": ref_id,
        }
    finally:
        service_client.table("jobs").delete().eq("id", job_id).execute()
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def _rows(parents: dict[str, str], user_id: str) -> dict[str, dict[str, object]]:
    """One insertable row per converted table, owned by ``user_id``."""
    return {
        "user_targets": {
            "user_id": user_id,
            "target_id": parents["target_id"],
        },
        "job_feedback": {
            "user_id": user_id,
            "job_posting_id": parents["job_id"],
            "target_id": parents["target_id"],
            "signal": "relevant",
        },
        "contribution_votes": {
            "user_id": user_id,
            "reference_jd_id": parents["ref_id"],
            "value": 1,
        },
        "target_learning_log": {
            "user_id": user_id,
            "target_id": parents["target_id"],
            "status": "applied",
            "prev_profile": {},
            "next_profile": {},
            "diff": {},
            "confidence": 0.5,
        },
    }


@pytest.mark.parametrize(
    "table",
    ["user_targets", "job_feedback", "contribution_votes", "target_learning_log"],
)
def test_orphan_user_id_rejected_23503(
    service_client: Client, seeded_parents: dict[str, str], table: str
) -> None:
    """A syntactically-valid uuid with no auth.users row must be refused —
    even by the RLS-bypassing service-role client."""
    orphan = str(uuid.uuid4())
    row = _rows(seeded_parents, orphan)[table]
    with pytest.raises(APIError) as exc:
        service_client.table(table).insert(row).execute()
    assert exc.value.code == "23503", f"{table}: expected FK violation, got {exc.value}"


def test_auth_user_delete_cascades_all_four_tables(
    service_client: Client, seeded_parents: dict[str, str]
) -> None:
    """Rows owned by a real auth user insert fine, then vanish when the
    auth user is deleted — no per-user row outlives its owner."""
    uid = create_auth_user(service_client)
    tables = list(_rows(seeded_parents, uid))
    try:
        for table, row in _rows(seeded_parents, uid).items():
            service_client.table(table).insert(row).execute()
        for table in tables:
            got = (
                service_client.table(table)
                .select("user_id")
                .eq("user_id", uid)
                .execute()
                .data
            )
            assert len(got) == 1, f"{table}: seed row missing before delete"
    finally:
        delete_auth_user(service_client, uid)

    for table in tables:
        leftover = (
            service_client.table(table)
            .select("user_id")
            .eq("user_id", uid)
            .execute()
            .data
        )
        assert leftover == [], f"{table}: row survived auth-user delete: {leftover}"
