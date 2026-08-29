"""#928 — a heterogeneous bulk upsert must not blank the keys some rows omit.

Runs against a LIVE local Supabase stack, deliberately. The bug is in the SQL
PostgREST generates from a bulk payload, not in our Python: a mocked client
records whatever dicts we hand it and would pass just as happily against the
broken code, so a hermetic test here would be a test that cannot fail. The
poller-side homogeneity guard lives in ``tests/test_poller.py``; this is the
one that shows what Postgres actually stores.

Self-skips when the stack isn't reachable; the default suite deselects the
``integration`` marker.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from supabase import Client

from app.services import db_write
from app.services.db_write import poll_db_upsert

pytestmark = pytest.mark.integration

ON_CONFLICT = "source_id,external_id"
COMPANY = "Upsert Contract Co"


@pytest.fixture
def source_id(service_client: Client) -> Iterator[str]:
    """A throwaway source to hang test postings off (``jobs.source_id`` is a
    NOT-NULL FK). Torn down with its jobs, pass or fail."""
    token = f"test-928-{uuid.uuid4().hex[:12]}"
    row = (
        service_client.table("sources")
        .insert({"board_token": token, "company_name": COMPANY, "provider": "greenhouse"})
        .execute()
        .data[0]
    )
    try:
        yield str(row["id"])
    finally:
        with contextlib.suppress(Exception):
            service_client.table("jobs").delete().eq("source_id", row["id"]).execute()
        with contextlib.suppress(Exception):
            service_client.table("sources").delete().eq("id", row["id"]).execute()


@pytest.fixture
def _sync_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``poll_db_upsert`` to its sync-client path so the fixture's
    ``service_client`` is the one that talks to the stack. The grouping under
    test is backend-independent — the async/sync seam has its own tests."""
    monkeypatch.setattr(db_write, "get_async_supabase", lambda: None)


def _row(source_id: str, external_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "external_id": external_id,
        "source_id": source_id,
        "title": f"Role {external_id}",
        "company_name": COMPANY,
        **extra,
    }


def _stored(client: Client, source_id: str) -> dict[str, dict[str, Any]]:
    rows = (
        client.table("jobs")
        .select("external_id, is_remote, employment_type")
        .eq("source_id", source_id)
        .execute()
        .data
        or []
    )
    return {r["external_id"]: r for r in rows}


def _seed(client: Client, source_id: str, **by_id: dict[str, Any]) -> None:
    """Seed each posting in its OWN statement, so the seeding itself can't be
    the thing that blanks a column."""
    for external_id, cols in by_id.items():
        client.table("jobs").upsert(
            [_row(source_id, external_id, **cols)], on_conflict=ON_CONFLICT
        ).execute()


def test_raw_bulk_upsert_blanks_a_key_a_sibling_supplies(
    service_client: Client, source_id: str
) -> None:
    """Pin the PostgREST behaviour the fix exists for. Without this, the tests
    below could pass against an upsert that was never broken in the first
    place. If this ever fails, PostgREST changed and ``poll_db_upsert`` may no
    longer be needed — read it as news, not as a regression."""
    _seed(service_client, source_id, silent={"is_remote": False}, speaks={})

    before = _stored(service_client, source_id)
    assert before["silent"]["is_remote"] is False  # PRECONDITION: really stored
    assert before["speaks"]["is_remote"] is None

    # ONE bulk statement: ``silent`` omits the key, ``speaks`` supplies it.
    service_client.table("jobs").upsert(
        [_row(source_id, "silent"), _row(source_id, "speaks", is_remote=True)],
        on_conflict=ON_CONFLICT,
    ).execute()

    after = _stored(service_client, source_id)
    assert after["speaks"]["is_remote"] is True
    assert after["silent"]["is_remote"] is None, (
        "PostgREST no longer applies the batch's union of keys to every row"
    )


def test_raw_bulk_upsert_rejects_a_duplicate_conflict_key(
    service_client: Client, source_id: str
) -> None:
    """The behaviour ``poll_db_upsert``'s uniqueness guard exists to PRESERVE.

    Two rows sharing ``(source_id, external_id)`` in one statement are a
    cardinality error. Grouping would split them across statements when their
    key-sets differ, and both would then succeed — so the guard has to reproduce
    this failure itself. Pinning it here proves the guard isn't inventing a
    restriction Postgres doesn't have."""
    with pytest.raises(Exception) as exc:
        service_client.table("jobs").upsert(
            [_row(source_id, "dup", is_remote=True), _row(source_id, "dup")],
            on_conflict=ON_CONFLICT,
        ).execute()
    assert "second time" in str(exc.value).lower(), str(exc.value)


async def test_grouped_upsert_rejects_a_duplicate_conflict_key(
    service_client: Client, source_id: str, _sync_only: None
) -> None:
    """End to end against the live stack: the helper raises before writing, so
    the split can't turn that cardinality error into last-write-wins."""
    rows = [
        _row(source_id, "dup", is_remote=True),
        _row(source_id, "dup"),  # same conflict key, different key-set
    ]
    assert len({frozenset(r) for r in rows}) == 2  # PRECONDITION: would split

    with pytest.raises(ValueError, match="duplicate"):
        await poll_db_upsert(
            service_client, table="jobs", rows=rows, on_conflict=ON_CONFLICT, label="t"
        )
    # And nothing landed — the guard runs before the first statement.
    assert _stored(service_client, source_id) == {}


@pytest.mark.parametrize("silent_first", [True, False])
async def test_grouped_upsert_leaves_an_omitted_is_remote_alone(
    service_client: Client, source_id: str, _sync_only: None, silent_first: bool
) -> None:
    """The headline: a board-silent posting keeps the ``is_remote`` another
    path established, even when a sibling in the same batch states its own.

    Both input orders — the blanking is the union of the batch's keys, not
    "last row wins", and order-independence is what proves that.
    """
    _seed(service_client, source_id, silent={"is_remote": False}, speaks={})

    before = _stored(service_client, source_id)
    assert before["silent"]["is_remote"] is False  # PRECONDITION: really stored
    assert before["speaks"]["is_remote"] is None

    rows = [_row(source_id, "silent"), _row(source_id, "speaks", is_remote=True)]
    if not silent_first:
        rows.reverse()
    assert len({frozenset(r) for r in rows}) == 2  # PRECONDITION: heterogeneous

    returned = await poll_db_upsert(
        service_client, table="jobs", rows=rows, on_conflict=ON_CONFLICT, label="t"
    )

    after = _stored(service_client, source_id)
    assert after["silent"]["is_remote"] is False, "the omitted key was blanked"
    assert after["speaks"]["is_remote"] is True
    # RETURNING still covers the whole batch — the scoring stages iterate it.
    assert sorted(r["external_id"] for r in returned) == ["silent", "speaks"]


@pytest.mark.parametrize("silent_first", [True, False])
async def test_grouped_upsert_leaves_an_omitted_employment_type_alone(
    service_client: Client, source_id: str, _sync_only: None, silent_first: bool
) -> None:
    """The second conditional column ``board_columns`` writes. Ashby/Lever
    publish a commitment string; Greenhouse doesn't, so the same batch mixes
    them."""
    _seed(service_client, source_id, silent={"employment_type": "contract"}, speaks={})

    before = _stored(service_client, source_id)
    assert before["silent"]["employment_type"] == "contract"  # PRECONDITION
    assert before["speaks"]["employment_type"] is None

    rows = [
        _row(source_id, "silent"),
        _row(source_id, "speaks", employment_type="full_time"),
    ]
    if not silent_first:
        rows.reverse()

    await poll_db_upsert(
        service_client, table="jobs", rows=rows, on_conflict=ON_CONFLICT, label="t"
    )

    after = _stored(service_client, source_id)
    assert after["silent"]["employment_type"] == "contract", "the omitted key was blanked"
    assert after["speaks"]["employment_type"] == "full_time"


async def test_grouped_upsert_handles_three_key_sets_at_once(
    service_client: Client, source_id: str, _sync_only: None
) -> None:
    """A real batch mixes both optional columns independently: some postings
    state remoteness, some a commitment, some both, some neither."""
    _seed(
        service_client,
        source_id,
        neither={"is_remote": False, "employment_type": "contract"},
        remote_only={"employment_type": "part_time"},
        both={},
        neither2={"is_remote": True, "employment_type": "temporary"},
    )

    before = _stored(service_client, source_id)
    assert before["neither"] == {
        "external_id": "neither",
        "is_remote": False,
        "employment_type": "contract",
    }
    assert before["remote_only"]["employment_type"] == "part_time"
    assert before["neither2"]["employment_type"] == "temporary"

    # ``neither2`` shares ``neither``'s key-set but sits LAST, so the group
    # concatenation ([neither, neither2, remote_only, both]) differs from the
    # input order — without which the ordering assertion below could not fail.
    rows = [
        _row(source_id, "neither"),  # {}
        _row(source_id, "remote_only", is_remote=True),  # {is_remote}
        _row(source_id, "both", is_remote=True, employment_type="internship"),
        _row(source_id, "neither2"),  # {} again
    ]
    assert len({frozenset(r) for r in rows}) == 3  # PRECONDITION

    returned = await poll_db_upsert(
        service_client, table="jobs", rows=rows, on_conflict=ON_CONFLICT, label="t"
    )

    after = _stored(service_client, source_id)
    # Untouched keys survive...
    assert after["neither"]["is_remote"] is False
    assert after["neither"]["employment_type"] == "contract"
    assert after["neither2"]["is_remote"] is True
    assert after["neither2"]["employment_type"] == "temporary"
    assert after["remote_only"]["employment_type"] == "part_time"
    # ...and supplied keys land.
    assert after["remote_only"]["is_remote"] is True
    assert after["both"]["is_remote"] is True
    assert after["both"]["employment_type"] == "internship"
    # Splitting the batch must not reshuffle the result the scoring stages
    # iterate — Phase 2's daily-cap trim resolves residual ties by position.
    assert [r["external_id"] for r in returned] == [
        "neither",
        "remote_only",
        "both",
        "neither2",
    ]


async def test_grouped_upsert_still_inserts_new_rows(
    service_client: Client, source_id: str, _sync_only: None
) -> None:
    """Grouping must not change the INSERT half: a first-sighting posting still
    lands, and a column no row in its group supplied stays NULL rather than
    inheriting a value from another group."""
    rows = [
        _row(source_id, "new-silent"),
        _row(source_id, "new-speaks", is_remote=True),
    ]

    returned = await poll_db_upsert(
        service_client, table="jobs", rows=rows, on_conflict=ON_CONFLICT, label="t"
    )

    after = _stored(service_client, source_id)
    assert sorted(after) == ["new-silent", "new-speaks"]
    assert after["new-speaks"]["is_remote"] is True
    assert after["new-silent"]["is_remote"] is None
    assert len(returned) == 2
