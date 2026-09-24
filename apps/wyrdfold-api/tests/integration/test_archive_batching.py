"""#1107: archiving a set of listings, against real Postgres.

In plain terms: when the app marks a batch of job listings dead, it now does
that in several small database statements instead of one big one — and this
proves the small statements still add up to exactly the same result.

Why the change existed at all: stamping ``jobs.archived_at`` is not a plain
one-column write. ``jobs_sync_scores_denorm_au`` is an AFTER UPDATE trigger
that runs FOR EACH ROW, so one statement pays that trigger once per id, and
the trigger in turn rewrites every one of that job's ``scores`` rows. The cost
of an archive statement is therefore set by how many rows it carries, and
production measured the old 200-row statement at 7,647 ms against Postgres'
8-second limit. A killed one leaves listings that are gone showing on every
serving surface.

The unit tests pin the batching against a recording client. These pin the
things only a real database can answer:

* the denormalisation trigger still fires for every id, in every batch — the
  thing that would silently break if batching skipped rows;
* splitting the write does not split the timestamp;
* the ``updated_at`` difference between the callers is real, not an artifact
  of how the mocks are shaped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from supabase import AsyncClient, Client

from app.services import db_write
from app.services.db_write import ARCHIVE_WRITE_CHUNK, archive_job_ids

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture()
def _async_write_path(async_service_client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``poll_db_write``'s backend lookup at the live async client.

    Without this the seam finds no pooled client, falls back to its sync path,
    and calls ``.execute()`` on an ASYNC builder — which returns a coroutine
    nobody awaits, so every write silently does nothing. Production always has
    the async client, so the sync fallback is not the path under test here.
    """
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: async_service_client)


# Enough ids to need several statements, and deliberately NOT a multiple of the
# batch size, so a short final batch is covered too.
_JOB_COUNT = ARCHIVE_WRITE_CHUNK * 2 + 7


@pytest.fixture()
def _corpus(service_client: Client) -> Any:
    """One source + target, ``_JOB_COUNT`` live jobs, one scores row each.

    The scores rows are the point: ``job_is_live`` on them is maintained by
    the trigger the archive write fires, so they are how "did every batch
    actually land" is observable rather than assumed.
    """
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    suffix = uuid.uuid4().hex[:8]
    job_ids = [str(uuid.uuid4()) for _ in range(_JOB_COUNT)]
    jobs = [
        {
            "id": jid,
            "external_id": f"arch-{suffix}-{n}",
            "source_id": source_id,
            "title": f"archive batching {suffix} {n}",
            "company_name": "Acme",
            "role_family": "engineering",
            "is_us": True,
            "cataloged_at": datetime.now(UTC).isoformat(),
        }
        for n, jid in enumerate(job_ids)
    ]
    try:
        service_client.table("sources").insert(
            {
                "id": source_id,
                "board_token": f"arch-{suffix}",
                "company_name": "Acme",
                "provider": "greenhouse",
            }
        ).execute()
        service_client.table("targets").insert(
            {"id": target_id, "label": f"Archive Target {suffix}", "role_family": "engineering"}
        ).execute()
        service_client.table("jobs").insert(jobs).execute()
        service_client.table("scores").insert(
            [
                {
                    "id": str(uuid.uuid4()),
                    "job_posting_id": jid,
                    "target_id": target_id,
                    "score": 70,
                    "scoring_status": "complete",
                }
                for jid in job_ids
            ]
        ).execute()
        yield {"job_ids": job_ids, "target_id": target_id, "source_id": source_id}
    finally:
        service_client.table("scores").delete().eq("target_id", target_id).execute()
        service_client.table("jobs").delete().eq("source_id", source_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()
        service_client.table("sources").delete().eq("id", source_id).execute()


async def test_batched_archive_lands_every_row_and_syncs_the_denorm(
    async_service_client: AsyncClient,
    service_client: Client,
    _corpus: Any,
    _async_write_path: None,
) -> None:
    """Every id is archived and every scores row follows, across all batches.

    This is the assertion that would catch a batching bug that silently drops
    a chunk: the rows in the dropped chunk would stay live, and their scores
    rows would keep ``job_is_live = true`` and go on being served.
    """
    job_ids: list[str] = _corpus["job_ids"]
    target_id: str = _corpus["target_id"]

    # PRECONDITION — without this the assertions below could pass vacuously on
    # a corpus that was never live in the first place.
    pre = service_client.table("scores").select("job_is_live").eq("target_id", target_id).execute()
    assert len(pre.data) == _JOB_COUNT
    assert all(r["job_is_live"] is True for r in pre.data), "corpus did not start live"

    stamp = datetime.now(UTC).isoformat()
    stamped = await archive_job_ids(
        async_service_client, job_ids, archived_at=stamp, label="integration archive"
    )

    assert stamped == _JOB_COUNT

    jobs_after = service_client.table("jobs").select("id, archived_at").in_("id", job_ids).execute()
    assert len(jobs_after.data) == _JOB_COUNT
    assert all(r["archived_at"] is not None for r in jobs_after.data), (
        "a batch did not land — some listings are still live"
    )

    # One timestamp across every batch, read back from Postgres rather than
    # from what we sent: listings that went dead together read that way.
    assert len({r["archived_at"] for r in jobs_after.data}) == 1

    # The per-row trigger fired for every id in every batch.
    scores_after = (
        service_client.table("scores").select("job_is_live").eq("target_id", target_id).execute()
    )
    assert len(scores_after.data) == _JOB_COUNT
    assert all(r["job_is_live"] is False for r in scores_after.data), (
        "the denormalisation trigger did not follow every batch"
    )


async def test_updated_at_moves_only_for_the_caller_that_asks(
    async_service_client: AsyncClient,
    service_client: Client,
    _corpus: Any,
    _async_write_path: None,
) -> None:
    """``jobs.updated_at`` has no trigger behind it, so whether it moves is
    decided purely by the payload — and the callers have always disagreed.
    Only the stale-listing sweep ever wrote it. Splitting one statement into
    several must not quietly change that either way."""
    job_ids: list[str] = _corpus["job_ids"]
    half = len(job_ids) // 2
    plain, sweep = job_ids[:half], job_ids[half:]

    before = {
        r["id"]: r["updated_at"]
        for r in service_client.table("jobs")
        .select("id, updated_at")
        .in_("id", job_ids)
        .execute()
        .data
    }

    stamp = datetime.now(UTC).isoformat()
    await archive_job_ids(async_service_client, plain, archived_at=stamp, label="plain")
    await archive_job_ids(
        async_service_client, sweep, archived_at=stamp, label="sweep", stamp_updated_at=True
    )

    after = {
        r["id"]: (r["archived_at"], r["updated_at"])
        for r in service_client.table("jobs")
        .select("id, archived_at, updated_at")
        .in_("id", job_ids)
        .execute()
        .data
    }

    # Both groups are archived...
    assert all(after[j][0] is not None for j in job_ids)
    # ...but only the sweep group's updated_at moved to the archive stamp.
    assert all(after[j][1] == before[j] for j in plain), "updated_at moved without being asked"
    assert all(after[j][1] != before[j] for j in sweep), "the sweep stopped stamping updated_at"
