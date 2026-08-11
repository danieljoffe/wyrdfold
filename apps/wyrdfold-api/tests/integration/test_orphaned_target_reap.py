"""Integration tests for the orphaned-target reap (#667).

Unit tests can prove the router calls the RPC. Only a live Postgres can prove
the thing that actually matters here: that the guard is correct in SQL, and that
removing the row really does cascade its scores away rather than leaving them
parented to nothing.

The reap is deliberately narrow, and the two ways it could be WRONG are both
worse than the bug it fixes:
  - too eager  => it deletes a target a co-follower is still using, cascading
                  away their scores. That is the audit-#29 H1 failure.
  - too eager  => it deletes an ops-sponsored catalog target that legitimately
                  has zero followers (#543).
So most of this file is about what the reap must NOT touch.

Self-skips when the local stack is unreachable (see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def _mk_target(service_client: Client, *, label: str, app_active: bool = False) -> str:
    target_id = str(uuid.uuid4())
    service_client.table("targets").insert(
        {"id": target_id, "label": label, "app_active": app_active}
    ).execute()
    return target_id


def _link(service_client: Client, target_id: str, user_id: str) -> None:
    service_client.table("user_targets").insert(
        {"user_id": user_id, "target_id": target_id, "is_active": True}
    ).execute()


def _unlink(service_client: Client, target_id: str, user_id: str) -> None:
    service_client.table("user_targets").delete().eq("user_id", user_id).eq(
        "target_id", target_id
    ).execute()


def _exists(service_client: Client, target_id: str) -> bool:
    resp = service_client.table("targets").select("id").eq("id", target_id).execute()
    return bool(resp.data)


def _reap(service_client: Client, target_id: str) -> bool:
    return bool(
        service_client.rpc("reap_orphaned_target", {"p_target_id": target_id}).execute().data
    )


@pytest.fixture
def cleanup_targets(service_client: Client) -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    for tid in created:
        service_client.table("targets").delete().eq("id", tid).execute()


def test_reaps_a_target_whose_last_follower_left(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    user_a_id, _ = two_seeded_users
    target_id = _mk_target(service_client, label=f"Reap Me {uuid.uuid4()}")
    cleanup_targets.append(target_id)
    _link(service_client, target_id, user_a_id)

    # Still followed → untouched.
    assert _reap(service_client, target_id) is False
    assert _exists(service_client, target_id) is True

    _unlink(service_client, target_id, user_a_id)

    assert _reap(service_client, target_id) is True
    assert _exists(service_client, target_id) is False


def test_never_reaps_while_a_co_follower_remains(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """THE dangerous case. Targets are a shared catalog, so one user unlinking
    must never take the row (and every co-follower's scores) with it."""
    user_a_id, user_b_id = two_seeded_users
    target_id = _mk_target(service_client, label=f"Shared {uuid.uuid4()}")
    cleanup_targets.append(target_id)
    _link(service_client, target_id, user_a_id)
    _link(service_client, target_id, user_b_id)

    _unlink(service_client, target_id, user_a_id)

    assert _reap(service_client, target_id) is False
    assert _exists(service_client, target_id) is True

    # ...and once the second one goes, it is genuinely orphaned.
    _unlink(service_client, target_id, user_b_id)
    assert _reap(service_client, target_id) is True
    assert _exists(service_client, target_id) is False


def test_never_reaps_an_ops_sponsored_catalog_target(
    service_client: Client, cleanup_targets: list[str]
) -> None:
    """`app_active` is a standing sponsorship floor that outlives followers
    (#543) — a seeded catalog target legitimately has zero memberships."""
    target_id = _mk_target(service_client, label=f"Catalog {uuid.uuid4()}", app_active=True)
    cleanup_targets.append(target_id)

    assert _reap(service_client, target_id) is False
    assert _exists(service_client, target_id) is True


def test_reap_cascades_the_orphaned_scores(
    service_client: Client, two_seeded_users: tuple[str, str], cleanup_targets: list[str]
) -> None:
    """The actual cost of an orphan is its score rows — one prod orphan held
    6,163, ~3.8% of the whole table. Removing the target must take them."""
    user_a_id, _ = two_seeded_users
    source_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    target_id = _mk_target(service_client, label=f"Scored {uuid.uuid4()}")
    cleanup_targets.append(target_id)
    service_client.table("sources").insert(
        {
            "id": source_id,
            "board_token": f"tok-{source_id[:8]}",
            "company_name": "Acme",
            "provider": "greenhouse",
        }
    ).execute()
    service_client.table("jobs").insert(
        {
            "id": job_id,
            "source_id": source_id,
            "external_id": f"ext-{job_id[:8]}",
            "title": "Orphan Scorer",
            "company_name": "Acme",
        }
    ).execute()
    service_client.table("scores").insert(
        {"job_posting_id": job_id, "target_id": target_id, "score": 80, "excluded": False}
    ).execute()
    _link(service_client, target_id, user_a_id)

    def score_count() -> int:
        return len(
            service_client.table("scores").select("id").eq("target_id", target_id).execute().data
            or []
        )

    assert score_count() == 1

    _unlink(service_client, target_id, user_a_id)
    assert _reap(service_client, target_id) is True

    assert score_count() == 0, "reaping the target left its scores parented to nothing"

    service_client.table("jobs").delete().eq("id", job_id).execute()
    service_client.table("sources").delete().eq("id", source_id).execute()


def test_reap_is_idempotent_and_safe_on_a_missing_id(service_client: Client) -> None:
    """Called twice, or on a ghost id — must be a quiet no-op, not an error."""
    assert _reap(service_client, str(uuid.uuid4())) is False
