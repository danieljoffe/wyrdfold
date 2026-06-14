"""Integration test for the operator/global view after the jobs.status drop
(#75 C4).

The legacy ``jobs.status`` column is gone — the operator path (api-key, no
JWT) now distinguishes only live vs globally-archived via ``jobs.archived_at``.
This test proves the underlying query shape against the live local stack:

  - ``archived_at IS NULL`` returns only the live job (the default operator
    view); the archived job is excluded.
  - ``archived_at IS NOT NULL`` returns only the archived job (the operator
    ``status='archived'`` audit view).
  - ``jobs.status`` no longer exists (selecting it errors).

Self-skips when the local Supabase stack is unreachable (see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_live_and_archived(
    service_client: Client,
) -> Iterator[tuple[str, str]]:
    """Seed one source + two jobs: one live (archived_at NULL) and one
    globally archived (archived_at set). Yields ``(live_id, archived_id)``
    and cleans up via the source cascade."""
    source_id = str(uuid.uuid4())
    live_id = str(uuid.uuid4())
    archived_id = str(uuid.uuid4())
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
            [
                {
                    "id": live_id,
                    "external_id": "ext-live",
                    "source_id": source_id,
                    "title": "Live Job",
                    "company_name": "Acme",
                    # archived_at left NULL -> globally live.
                },
                {
                    "id": archived_id,
                    "external_id": "ext-archived",
                    "source_id": source_id,
                    "title": "Archived Job",
                    "company_name": "Acme",
                    "archived_at": datetime.now(UTC).isoformat(),
                },
            ]
        ).execute()
        yield live_id, archived_id
    finally:
        # source cascade removes both jobs.
        service_client.table("sources").delete().eq("id", source_id).execute()


def test_operator_live_view_excludes_archived(
    service_client: Client,
    seeded_live_and_archived: tuple[str, str],
) -> None:
    """Default operator view (archived_at IS NULL) returns only the live job."""
    live_id, archived_id = seeded_live_and_archived

    resp = (
        service_client.table("jobs")
        .select("id, archived_at")
        .in_("id", [live_id, archived_id])
        .is_("archived_at", "null")
        .execute()
    )
    ids = {r["id"] for r in (resp.data or [])}
    assert ids == {live_id}, f"live view should show only the live job, got {ids!r}"


def test_operator_archived_audit_view(
    service_client: Client,
    seeded_live_and_archived: tuple[str, str],
) -> None:
    """Operator status='archived' audit view returns only the archived job."""
    live_id, archived_id = seeded_live_and_archived

    resp = (
        service_client.table("jobs")
        .select("id, archived_at")
        .in_("id", [live_id, archived_id])
        .not_.is_("archived_at", "null")
        .execute()
    )
    ids = {r["id"] for r in (resp.data or [])}
    assert ids == {
        archived_id
    }, f"archived view should show only the archived job, got {ids!r}"


def test_jobs_status_column_is_gone(
    service_client: Client,
    seeded_live_and_archived: tuple[str, str],
) -> None:
    """The legacy ``jobs.status`` column was dropped in #75 C4 — selecting it
    must error."""
    live_id, _archived_id = seeded_live_and_archived
    with pytest.raises(Exception):
        service_client.table("jobs").select("status").eq(
            "id", live_id
        ).execute()
