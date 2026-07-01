"""Integration test for the Pending-aware score floor (#47 finding #4).

Unit tests cover the Python logic with a mocked PostgREST chain; the one thing
they can't prove is that the ``_apply_score_floor`` OR-expression
(``scoring_status.is.null,scoring_status.neq.complete,score.gte.N``) is valid
PostgREST that the real database actually evaluates as intended. This runs the
real query against the live local Supabase stack: a graded row below the floor
is dropped, while a not-yet-graded ("Pending") row below the floor is exempt and
returned. Self-skips when the stack is unreachable (see conftest).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from supabase import Client

from app.routers.jobs import _apply_score_floor

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_floor_scores(service_client: Client) -> Iterator[tuple[str, str, str]]:
    """One target with two scores below a floor of 50: a graded (``complete``)
    job at 40 and a Pending (``stage2``) job at 30. Yields
    ``(target_id, graded_job_id, pending_job_id)``; cleans up via the source
    cascade + explicit target delete."""
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    graded_job = str(uuid.uuid4())
    pending_job = str(uuid.uuid4())
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
                    "id": graded_job,
                    "external_id": "ext-graded",
                    "source_id": source_id,
                    "title": "Graded Job",
                    "company_name": "Acme",
                },
                {
                    "id": pending_job,
                    "external_id": "ext-pending",
                    "source_id": source_id,
                    "title": "Pending Job",
                    "company_name": "Acme",
                },
            ]
        ).execute()
        service_client.table("targets").insert(
            {"id": target_id, "label": "Floor Target"}
        ).execute()
        service_client.table("scores").insert(
            [
                # Graded (real fit score) at 40 — below a 50 floor, must drop.
                {
                    "job_posting_id": graded_job,
                    "target_id": target_id,
                    "score": 40,
                    "excluded": False,
                    "scoring_status": "complete",
                },
                # Pending (keyword placeholder) at 30 — below the floor, but
                # exempt because it has no real fit score yet.
                {
                    "job_posting_id": pending_job,
                    "target_id": target_id,
                    "score": 30,
                    "excluded": False,
                    "scoring_status": "stage2",
                },
            ]
        ).execute()
        yield target_id, graded_job, pending_job
    finally:
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def test_floor_exempts_pending_drops_low_graded(
    service_client: Client,
    seeded_floor_scores: tuple[str, str, str],
) -> None:
    target_id, graded_job, pending_job = seeded_floor_scores

    query = (
        service_client.table("scores")
        .select("job_posting_id, score, scoring_status")
        .eq("target_id", target_id)
        .eq("excluded", False)
    )
    query = _apply_score_floor(query, 50)
    rows = query.execute().data or []
    returned = {r["job_posting_id"] for r in rows}

    # The Pending row is exempt from the fit floor; the low graded row is dropped.
    assert pending_job in returned
    assert graded_job not in returned
