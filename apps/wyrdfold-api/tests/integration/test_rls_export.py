"""Data-export dual-client equivalence (#88 — GET /profile/export flip).

The export moved from all-service-role onto the caller's RLS user client
for every self-scoped table + Storage, keeping a service-role client only
for the reads RLS would silently truncate (``user_api_keys``,
``notifications_sent``, ``reference_jds``). Against the live stack this
proves the three claims that justify the flip:

* **Equivalence** — the dual-client export is row-for-row identical to
  the pre-flip all-service-role export (same tables, same rows, same ZIP
  inventory), including the user's contribution on a target they no
  longer follow. Any RLS policy that silently drops a row breaks this.
* **The carve-out is necessary** — read on the user client, the gap
  tables really do break: user_api_keys / notifications_sent hard-fail
  (42501, no authenticated grant) and reference_jds silently loses the
  unfollowed-target contribution — both failure modes proven to happen.
* **The backstop is real** — another user's client collecting with the
  victim's user_id gets zero rows from every RLS-riding table and no
  Storage listing: Postgres, not the ``.eq()`` filter, is the control.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from supabase import Client

from app.services.data_export import build_export_zip, collect_user_data

pytestmark = pytest.mark.integration

_BUCKET = "resume-uploads"


@pytest.fixture
def seeded_export_user(
    service_client: Client, two_seeded_users: tuple[str, str]
) -> Iterator[tuple[str, str]]:
    """User A with rows across the export inventory, including the edge
    case: a reference_jd they contributed on a target they do NOT follow."""
    uid_a, uid_b = two_seeded_users
    source_id = (
        service_client.table("sources")
        .insert({"board_token": "export-int", "company_name": "Export Co"})
        .execute()
        .data[0]["id"]
    )
    followed = (
        service_client.table("targets")
        .insert({"label": "Export Followed Target"})
        .execute()
        .data[0]["id"]
    )
    unfollowed = (
        service_client.table("targets")
        .insert({"label": "Export Unfollowed Target"})
        .execute()
        .data[0]["id"]
    )
    job_id = (
        service_client.table("jobs")
        .insert(
            {
                "external_id": "export-int-1",
                "source_id": source_id,
                "title": "Engineer",
                "company_name": "Export Co",
            }
        )
        .execute()
        .data[0]["id"]
    )
    service_client.table("user_targets").insert(
        {"user_id": uid_a, "target_id": followed}
    ).execute()
    ref_followed = (
        service_client.table("reference_jds")
        .insert(
            {
                "target_id": followed,
                "jd_text": "followed-target contribution",
                "extracted_profile": {},
                "user_id": uid_a,
            }
        )
        .execute()
        .data[0]["id"]
    )
    # The edge case: A's contribution on a target they don't follow —
    # invisible to reference_jds_follower_read, still theirs to export.
    service_client.table("reference_jds").insert(
        {
            "target_id": unfollowed,
            "jd_text": "unfollowed-target contribution",
            "extracted_profile": {},
            "user_id": uid_a,
        }
    ).execute()
    service_client.table("job_feedback").insert(
        {
            "user_id": uid_a,
            "job_posting_id": job_id,
            "target_id": followed,
            "signal": "relevant",
        }
    ).execute()
    service_client.table("target_learning_log").insert(
        {
            "user_id": uid_a,
            "target_id": followed,
            "status": "applied",
            "prev_profile": {},
            "next_profile": {},
            "diff": {},
            "confidence": 0.5,
        }
    ).execute()
    service_client.table("contribution_votes").insert(
        {"user_id": uid_a, "reference_jd_id": ref_followed, "value": 1}
    ).execute()
    service_client.table("user_api_keys").insert(
        {
            "user_id": uid_a,
            "provider": "openrouter",
            "ciphertext": "int-test-ciphertext",
            "last4": "ab12",
        }
    ).execute()
    service_client.table("scores").insert(
        {"job_posting_id": job_id, "target_id": followed, "score": 80}
    ).execute()
    profile_id = (
        service_client.table("user_profiles")
        .select("id")
        .eq("user_id", uid_a)
        .execute()
        .data[0]["id"]
    )
    service_client.table("notifications_sent").insert(
        {"user_profile_id": profile_id, "job_posting_id": job_id, "score_at_send": 80}
    ).execute()
    path = f"{uid_a}/export-int-test.pdf"
    service_client.storage.from_(_BUCKET).upload(
        path=path,
        file=b"EXPORT-PDF",
        file_options={"content-type": "application/octet-stream", "upsert": "true"},
    )
    try:
        yield uid_a, uid_b
    finally:
        # two_seeded_users deletes the auth users (cascading their rows);
        # clean the shared-catalog seeds + storage explicitly.
        service_client.storage.from_(_BUCKET).remove([path])
        service_client.table("jobs").delete().eq("id", job_id).execute()
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().in_(
            "id", [followed, unfollowed]
        ).execute()


def _normalized(data: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """Order-insensitive, comparable form of a collected export."""
    return {
        table: sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
        for table, rows in data.items()
    }


def test_dual_client_export_identical_to_service_role(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    seeded_export_user: tuple[str, str],
) -> None:
    """Row-for-row equivalence: flipping the per-user reads onto the RLS
    user client must not change the export by a single row."""
    uid_a, _ = seeded_export_user
    flipped = collect_user_data(
        user_client_factory(uid_a), user_id=uid_a, service_supabase=service_client
    )
    pre_flip = collect_user_data(
        service_client, user_id=uid_a, service_supabase=service_client
    )
    assert _normalized(flipped) == _normalized(pre_flip)
    # The seeds actually landed — an empty==empty pass would prove nothing.
    for table in (
        "user_targets",
        "job_feedback",
        "target_learning_log",
        "contribution_votes",
        "user_api_keys",
        "notifications_sent",
        "scores",
    ):
        assert flipped[table], f"{table}: seed row missing from export"
    # Both contributions present — including the unfollowed-target one that
    # only the service-role carve-out can see.
    assert {r["jd_text"] for r in flipped["reference_jds"]} == {
        "followed-target contribution",
        "unfollowed-target contribution",
    }


def test_zip_inventory_identical_across_clients(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    seeded_export_user: tuple[str, str],
) -> None:
    """End-to-end ZIP diff: same file inventory (incl. Storage objects) and
    same data.json either way."""
    import io
    import zipfile

    uid_a, _ = seeded_export_user
    flipped = build_export_zip(
        user_client_factory(uid_a), user_id=uid_a, service_supabase=service_client
    )
    pre_flip = build_export_zip(
        service_client, user_id=uid_a, service_supabase=service_client
    )
    zf_flipped = zipfile.ZipFile(io.BytesIO(flipped))
    zf_pre = zipfile.ZipFile(io.BytesIO(pre_flip))
    assert sorted(zf_flipped.namelist()) == sorted(zf_pre.namelist())
    assert f"files/{_BUCKET}/export-int-test.pdf" in zf_flipped.namelist()
    assert _normalized(json.loads(zf_flipped.read("data.json"))) == _normalized(
        json.loads(zf_pre.read("data.json"))
    )


def test_gap_tables_break_on_the_user_client(
    user_client_factory: Callable[[str], Client],
    seeded_export_user: tuple[str, str],
) -> None:
    """The carve-out is NECESSARY, proven per failure mode on the caller's
    own RLS client: user_api_keys and notifications_sent have no
    ``authenticated`` grant at all (42501 — the export would 500), and
    reference_jds is follower-scoped, so the unfollowed-target contribution
    silently vanishes (the export would lose the user's data)."""
    from postgrest.exceptions import APIError

    uid_a, _ = seeded_export_user
    client_a = user_client_factory(uid_a)
    with pytest.raises(APIError) as exc:
        client_a.table("user_api_keys").select("last4").eq("user_id", uid_a).execute()
    assert exc.value.code == "42501"
    with pytest.raises(APIError) as exc:
        client_a.table("notifications_sent").select("id").execute()
    assert exc.value.code == "42501"
    visible = {
        r["jd_text"]
        for r in client_a.table("reference_jds")
        .select("jd_text")
        .eq("user_id", uid_a)
        .execute()
        .data
    }
    assert visible == {"followed-target contribution"}
    assert "unfollowed-target contribution" not in visible


def test_rls_backstop_blocks_cross_user_export(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    seeded_export_user: tuple[str, str],
) -> None:
    """The flip's payoff: B's client collecting with A's user_id gets ZERO
    rows from every RLS-riding table and no Storage files — even though the
    ``.eq("user_id", uid_a)`` filters ask for A's data. Before the flip the
    service-role client would have returned everything."""
    uid_a, uid_b = seeded_export_user
    stolen = collect_user_data(
        user_client_factory(uid_b), user_id=uid_a, service_supabase=service_client
    )
    for table in (
        "user_targets",
        "job_feedback",
        "target_learning_log",
        "contribution_votes",
        "user_jobs",
        "user_profiles",
        "analyses",
        "llm_costs",
    ):
        assert stolen[table] == [], f"{table}: leaked to another user's client"
    # No profile row visible -> the notifications_sent service read never runs.
    assert "notifications_sent" not in stolen
    # And B's client cannot list A's Storage prefix.
    assert (
        user_client_factory(uid_b).storage.from_(_BUCKET).list(uid_a) or []
    ) == []
