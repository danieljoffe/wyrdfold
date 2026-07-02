"""RLS write-denial gate for the shared catalog (#6 / #79 R3).

Phase-1 (#79) gave `jobs` / `targets` / `scores` / `reference_jds` a permissive
SELECT policy (`USING (true)`) and DELIBERATELY no write policy — authenticated
users may read the shared catalog but every write is denied by RLS. That
deny-by-default is the backstop that stops a user from surfacing a job in
another tenant's list (by inserting a `scores` row) or mutating shared data.

These turn that "no write policy" claim into an executed regression: a
JWT-bound user client's INSERT raises an RLS violation, its UPDATE matches zero
rows (verified on disk via the service-role client), and the service-role-only
tables don't leak to the user client at all. If someone added a write policy or
wired the service-role client into a user write path, these start failing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from postgrest.exceptions import APIError
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_catalog(service_client: Client) -> Iterator[tuple[str, str, str, str]]:
    """Seed (via service role, bypassing RLS) a source + job + two targets, with
    a score on target_a only. Yields (target_a, target_b, posting_id, source_id);
    the source delete cascades to jobs/scores, targets are removed explicitly.
    """
    source_id = str(uuid.uuid4())
    target_a = str(uuid.uuid4())
    target_b = str(uuid.uuid4())
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
                "external_id": "ext-wd",
                "source_id": source_id,
                "title": "Shared Job",
                "company_name": "Acme",
            }
        ).execute()
        service_client.table("targets").insert(
            [{"id": target_a, "label": "WD Target A"}, {"id": target_b, "label": "WD Target B"}]
        ).execute()
        service_client.table("scores").insert(
            {"job_posting_id": posting_id, "target_id": target_a, "score": 50, "excluded": False}
        ).execute()
        yield target_a, target_b, posting_id, source_id
    finally:
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().in_("id", [target_a, target_b]).execute()


def test_user_cannot_insert_score(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
    seeded_catalog: tuple[str, str, str, str],
) -> None:
    """The privacy-critical one: a score row surfaces a job in a tenant's list
    (scores ⋈ user_targets). A user JWT must not be able to create one."""
    uid_a, _ = two_seeded_users
    _target_a, target_b, posting_id, _ = seeded_catalog
    client_a = user_client_factory(uid_a)

    with pytest.raises(APIError):  # new row violates row-level security policy
        client_a.table("scores").insert(
            {"job_posting_id": posting_id, "target_id": target_b, "score": 99, "excluded": False}
        ).execute()

    # Belt-and-suspenders: confirm on disk no score was created for target_b.
    rows = (
        service_client.table("scores")
        .select("job_posting_id")
        .eq("target_id", target_b)
        .execute()
        .data
    )
    assert rows == [], "RLS leak: user client created a shared scores row"


def test_user_cannot_update_score(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
    seeded_catalog: tuple[str, str, str, str],
) -> None:
    uid_a, _ = two_seeded_users
    target_a, _target_b, posting_id, _ = seeded_catalog
    client_a = user_client_factory(uid_a)

    resp = (
        client_a.table("scores")
        .update({"score": 1, "excluded": True})
        .eq("job_posting_id", posting_id)
        .eq("target_id", target_a)
        .execute()
    )
    assert resp.data == [], "RLS leak: user UPDATE matched a shared scores row"

    rows = (
        service_client.table("scores")
        .select("score, excluded")
        .eq("job_posting_id", posting_id)
        .eq("target_id", target_a)
        .execute()
        .data
    )
    assert rows and rows[0]["score"] == 50 and rows[0]["excluded"] is False, (
        "RLS leak: shared scores row was mutated by a user client"
    )


def test_user_cannot_insert_target(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
) -> None:
    uid_a, _ = two_seeded_users
    client_a = user_client_factory(uid_a)
    new_id = str(uuid.uuid4())

    with pytest.raises(APIError):
        client_a.table("targets").insert({"id": new_id, "label": "should be denied"}).execute()

    rows = service_client.table("targets").select("id").eq("id", new_id).execute().data
    assert rows == [], "RLS leak: user client created a shared targets row"


def test_service_role_only_table_does_not_leak_to_user(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    seeded_catalog: tuple[str, str, str, str],
) -> None:
    """`sources` had its anon/authenticated grant revoked (#79/#111) — a user
    client must neither read nor write it. Accept either a hard permission
    error or an empty result; the invariant is "no leak"."""
    uid_a, _ = two_seeded_users
    _a, _b, _posting, source_id = seeded_catalog
    client_a = user_client_factory(uid_a)

    try:
        resp = client_a.table("sources").select("id").eq("id", source_id).execute()
        assert resp.data == [], "service-role-only table `sources` leaked to a user"
    except APIError:
        pass  # permission denied (grant revoked) is the stronger, also-acceptable outcome

    with pytest.raises(APIError):
        client_a.table("sources").insert(
            {
                "id": str(uuid.uuid4()),
                "board_token": f"x-{uuid.uuid4().hex[:8]}",
                "company_name": "Evil",
                "provider": "greenhouse",
            }
        ).execute()


def test_user_cannot_insert_job(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
    seeded_catalog: tuple[str, str, str, str],
) -> None:
    """A user must not be able to plant a posting in the shared catalog —
    every tenant's list is built from `jobs`."""
    uid_a, _ = two_seeded_users
    _a, _b, _posting, source_id = seeded_catalog
    client_a = user_client_factory(uid_a)
    new_id = str(uuid.uuid4())

    with pytest.raises(APIError):
        client_a.table("jobs").insert(
            {
                "id": new_id,
                "external_id": "ext-evil",
                "source_id": source_id,
                "title": "Planted Job",
                "company_name": "Evil Corp",
            }
        ).execute()

    rows = service_client.table("jobs").select("id").eq("id", new_id).execute().data
    assert rows == [], "RLS leak: user client created a shared jobs row"


def test_user_cannot_update_job(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
    seeded_catalog: tuple[str, str, str, str],
) -> None:
    uid_a, _ = two_seeded_users
    _a, _b, posting_id, _src = seeded_catalog
    client_a = user_client_factory(uid_a)

    resp = (
        client_a.table("jobs")
        .update({"title": "Defaced"})
        .eq("id", posting_id)
        .execute()
    )
    assert resp.data == [], "RLS leak: user UPDATE matched a shared jobs row"

    rows = service_client.table("jobs").select("title").eq("id", posting_id).execute().data
    assert rows and rows[0]["title"] == "Shared Job", (
        "RLS leak: shared jobs row was mutated by a user client"
    )


@pytest.fixture
def seeded_reference_jd(
    service_client: Client,
    seeded_catalog: tuple[str, str, str, str],
    two_seeded_users: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """A reference JD on target_a, attributed to user B, with user A following
    target_a (so A can *read* it via the follower policy — the sharpest case
    for proving A still can't write it). Yields (target_a, ref_jd_id)."""
    target_a, _b, _posting, _src = seeded_catalog
    uid_a, uid_b = two_seeded_users
    ref_id = (
        service_client.table("reference_jds")
        .insert(
            {
                "target_id": target_a,
                "jd_text": "B's reference jd",
                "user_id": uid_b,
            }
        )
        .execute()
        .data[0]["id"]
    )
    service_client.table("user_targets").insert(
        {"user_id": uid_a, "target_id": target_a, "is_active": True}
    ).execute()
    try:
        yield target_a, ref_id
    finally:
        service_client.table("user_targets").delete().eq("target_id", target_a).execute()
        service_client.table("reference_jds").delete().eq("id", ref_id).execute()


def test_user_cannot_insert_reference_jd(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
    seeded_reference_jd: tuple[str, str],
) -> None:
    """reference_jds is SELECT-only for authenticated (follower-scoped read,
    20260623120000); the write path is the ownership-gated API. Direct
    PostgREST inserts must be refused — a planted JD would skew the target's
    consolidated scoring profile for every follower."""
    uid_a, _ = two_seeded_users
    target_a, _ref = seeded_reference_jd
    client_a = user_client_factory(uid_a)

    with pytest.raises(APIError):
        client_a.table("reference_jds").insert(
            {"target_id": target_a, "jd_text": "planted jd", "user_id": uid_a}
        ).execute()


def test_user_cannot_update_or_delete_reference_jd(
    two_seeded_users: tuple[str, str],
    user_client_factory: Callable[[str], Client],
    service_client: Client,
    seeded_reference_jd: tuple[str, str],
) -> None:
    """The #24 F1 IDOR (delete another user's reference JD) — proven dead at
    the DB layer: a follower can read the row, but UPDATE/DELETE match zero
    rows under RLS."""
    uid_a, _ = two_seeded_users
    _target, ref_id = seeded_reference_jd
    client_a = user_client_factory(uid_a)

    upd = client_a.table("reference_jds").update({"jd_text": "defaced"}).eq("id", ref_id).execute()
    assert upd.data == [], "RLS leak: user UPDATE matched another user's reference_jd"

    del_ = client_a.table("reference_jds").delete().eq("id", ref_id).execute()
    assert del_.data == [], "RLS leak: user DELETE matched another user's reference_jd"

    rows = (
        service_client.table("reference_jds").select("jd_text").eq("id", ref_id).execute().data
    )
    assert rows and rows[0]["jd_text"] == "B's reference jd", (
        "RLS leak: reference_jd was mutated/deleted by a non-owner"
    )
