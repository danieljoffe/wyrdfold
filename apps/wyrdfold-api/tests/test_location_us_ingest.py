"""The location string's own US verdict, applied at ingest.

Sibling of ``test_board_country_ingest``: same mechanism (stamp ``jobs.is_us``
after the upsert, from a free deterministic signal), different source of truth.
The board pass only reaches the 39.2% of enabled sources whose provider
publishes a structured country — Greenhouse and Workday, 60.8% between them,
publish none and never will. What they DO publish is a location string, and
``positively_us_location`` already reads it for admission and for the archive
veto; the conclusion was computed and thrown away rather than recorded.

The whole contract here is ONE-DIRECTIONAL. A wrong ``False`` hides a real job
from every serving surface (they all gate ``is_us IS NOT FALSE``) and, with
``QUALIFICATION_ARCHIVE_NON_US`` on, archives it irreversibly. A withheld
verdict only leaves the row where it already sat. So these tests care far more
about what is NOT written than about what is, and every "nothing was written"
assertion rides alongside a sibling posting that IS stamped in the same cycle —
a no-write assertion that cannot tell "correctly silent" from "the feature is
dead" proves nothing.

Sample locations are ones the poller's L1 gate really admits (asserted via the
``summary["new"]`` precondition in every cycle test), and several are verbatim
prod strings from the 5-day window measured before this shipped.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.services import poller as poller_mod
from app.services.standard_job import StandardJob
from tests.test_board_country_ingest import (
    _board_job,
    _payload_ids,
    _record_updates,
    _run_poll,
    _upserted_row,
    _written_row,
)
from tests.test_poller import _make_poll_supabase

pytestmark = pytest.mark.asyncio

# The live control every no-write test rides alongside: a board that says
# nothing and a location that plainly names the US, so it must be stamped.
_CONTROL_ID = "us-control"
_CONTROL_ROW_ID = "job-us-control"


def _location_job(
    location: str,
    *,
    external_id: str = "k3",
    title: str = "Staff Frontend Engineer",
    country: str | None = None,
) -> StandardJob:
    return _board_job(country=country, location=location, external_id=external_id, title=title)


def _control_job() -> StandardJob:
    return _location_job("Austin, TX", external_id=_CONTROL_ID, title="Senior Frontend Developer")


def _control_row() -> dict[str, Any]:
    return _upserted_row(row_id=_CONTROL_ROW_ID, external_id=_CONTROL_ID)


def _assert_control_was_stamped(pairs: list[tuple[dict[str, Any], list[str]]]) -> None:
    """The mechanism is alive in THIS cycle — without this, every no-write
    assertion below would pass just as happily against a deleted feature."""
    assert any(payload == {"is_us": True} and _CONTROL_ROW_ID in ids for payload, ids in pairs), (
        pairs
    )


async def test_a_plainly_us_location_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the change. Greenhouse publishes no country, so before this
    the row landed ``is_us = NULL`` even though its own location said Texas."""
    updates, jobs_table, summary = await _run_poll(
        monkeypatch, [_location_job("Austin, TX")], upserted=[_upserted_row()]
    )

    # Preconditions: the row really was ingested, so the write below means "the
    # location was read", not "the poll did nothing".
    assert summary["error"] is None
    assert summary["new"] == 1
    assert jobs_table.upsert.called

    assert _payload_ids(updates) == [({"is_us": True}, ["j1"])]


async def test_an_explicit_country_marker_is_recorded_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not just ``City, ST``: a bare "United States" is the second form
    ``positively_us_location`` recognizes, and it is 1 in 6 of the real strings
    this stamps ("Tampa, United States", "Remote (USA)")."""
    updates, _jobs_table, summary = await _run_poll(
        monkeypatch, [_location_job("Remote (USA)")], upserted=[_upserted_row()]
    )

    assert summary["new"] == 1
    assert _payload_ids(updates) == [({"is_us": True}, ["j1"])]


async def test_a_plainly_non_us_location_is_never_stamped_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ONE-DIRECTIONAL, the rule the whole design rests on.

    "Bolivia" is a real prod location that the permissive L1 admission gate lets
    through (it is not on the non-US hint list), so it reaches this pass with
    ``is_us = NULL``. It must leave with ``is_us = NULL``: writing ``False``
    from a location string would hide a job on the strength of a hint list that
    was never built to be exhaustive, and the archive it can trigger is one-way.
    """
    updates, _jobs_table, summary = await _run_poll(
        monkeypatch,
        [_location_job("Bolivia"), _control_job()],
        upserted=[_upserted_row(), _control_row()],
    )

    assert summary["new"] == 2  # precondition: BOTH rows were ingested
    pairs = _payload_ids(updates)
    _assert_control_was_stamped(pairs)
    assert all("j1" not in ids for _payload, ids in pairs), pairs
    # ...and no write anywhere in the cycle asserted a negative from a location.
    assert all(payload.get("is_us") is not False for payload, _ids in pairs), pairs


async def test_an_ambiguous_location_is_left_for_the_grader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Remote" is the case the permissive ``is_us_location`` calls US and this
    pass must not: its True means "not provably foreign", which is not a fact
    worth storing. Storing it would put the corpus's biggest ambiguous bucket
    on the wrong side of any future reader that trusts ``is_us = true``."""
    updates, _jobs_table, summary = await _run_poll(
        monkeypatch,
        [_location_job("Remote"), _control_job()],
        upserted=[_upserted_row(), _control_row()],
    )

    assert summary["new"] == 2
    pairs = _payload_ids(updates)
    _assert_control_was_stamped(pairs)
    assert all("j1" not in ids for _payload, ids in pairs), pairs


@pytest.mark.parametrize("stored", [True, False])
async def test_an_existing_verdict_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch, stored: bool
) -> None:
    """The upsert RETURNING carries the STORED verdict. A row the LLM tagger
    already judged — including one it judged NON-US — must be left exactly as it
    is: this pass is a filler of blanks, not a corrector of verdicts, and
    overwriting a ``False`` would silently undo a real archive decision."""
    updates, _jobs_table, summary = await _run_poll(
        monkeypatch,
        [_location_job("Austin, TX"), _control_job()],
        upserted=[_upserted_row(is_us=stored), _control_row()],
    )

    assert summary["new"] == 2
    pairs = _payload_ids(updates)
    _assert_control_was_stamped(pairs)
    assert all("j1" not in ids for _payload, ids in pairs), pairs


async def test_the_board_verdict_wins_and_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering: the board's structured country is the employer's own answer, so
    it runs first and this pass fills only what it left unknown.

    Both passes would write ``is_us = True`` here, and exactly one UPDATE must
    be issued — the board's, identifiable by carrying no WHERE filter. A second
    one would mean the location pass ignored the verdict the board just patched
    back, which on a row the board had marked NON-US is a silent reversal.
    """
    updates, _jobs_table, summary = await _run_poll(
        monkeypatch,
        [_location_job("Austin, TX", country="US"), _control_job()],
        upserted=[_upserted_row(), _control_row()],
    )

    assert summary["new"] == 2
    _assert_control_was_stamped(_payload_ids(updates))  # the pass IS alive this cycle
    j1_writes = [(u.payload, u.filters) for u in updates if "j1" in u.ids]
    assert j1_writes == [({"is_us": True}, [])], updates


async def test_the_write_re_asserts_that_the_row_is_still_untagged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The UPDATE carries ``.is_("is_us", "null")``.

    Without it, a row the LLM tagger graded BETWEEN the upsert snapshot and this
    write has its verdict overwritten — the read-then-write race the WHERE
    clause closes, and the one way a TRUE-only pass can still destroy a FALSE.
    The counter has to follow the WHERE clause: it reports rows PostgREST
    actually changed, read back off the response, never the candidate count.
    """
    row = _upserted_row()  # the snapshot says untagged...
    with caplog.at_level(logging.INFO, logger="app.services.poller"):
        updates, _jobs_table, summary = await _run_poll(
            monkeypatch,
            [_location_job("Austin, TX")],
            upserted=[row],
            db={"j1": {"archived_at": None, "is_us": False}},  # ...the DB disagrees
        )

    # Preconditions: the row ingested and the write WAS attempted, so
    # "marked=0" below is not true for the boring reason.
    assert summary["new"] == 1
    attempted = [u for u in updates if u.payload == {"is_us": True} and u.ids == ["j1"]]
    assert len(attempted) == 1, updates
    assert attempted[0].filters == [("is_us", "null")]

    assert "location_us_marked=0" in _funnel_line(caplog)
    assert row["is_us"] is None  # and the dict the grader reads is not told otherwise


def _funnel_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("poll_funnel ")]
    assert lines, "precondition: the cycle emitted its funnel line"
    return lines[0]


async def test_the_counter_reports_the_write_that_did_land(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Control for the test above: with no concurrent tagging the counter reads
    1. Without it, a counter hard-wired to zero would pass."""
    row = _upserted_row()
    with caplog.at_level(logging.INFO, logger="app.services.poller"):
        await _run_poll(monkeypatch, [_location_job("Austin, TX")], upserted=[row])

    assert "location_us_marked=1" in _funnel_line(caplog)
    assert row["is_us"] is True  # patched back for this cycle's Phase-2 gate


async def test_the_verdict_never_rides_the_bulk_upsert_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#928: a PostgREST bulk upsert builds ONE column list for the whole batch,
    so a key present on any row is written to every row and the rows that
    omitted it get NULL. ``is_us`` is decided per row, so putting it in the
    payload would blank the tagger's verdict on every silent sibling."""
    _updates, jobs_table, summary = await _run_poll(
        monkeypatch,
        [
            _location_job("Austin, TX"),
            # A distinct title: same (company, title) is a cross-posting dupe
            # and _dedupe_by_content would drop it before the upsert.
            _location_job("Bolivia", external_id="k4", title="Senior Frontend Developer"),
        ],
        upserted=[_upserted_row(), _upserted_row(row_id="j2", external_id="k4")],
    )

    # Preconditions: both rows rode ONE statement (``poll_db_upsert`` partitions
    # by key-set, so a split batch would make this assertion vacuous), and one
    # of them is the row the pass stamps.
    assert summary["new"] == 2
    assert jobs_table.upsert.call_count == 1, jobs_table.upsert.call_args_list
    written = _written_row(jobs_table, "k3")
    assert written["location"] == "Austin, TX"
    assert "is_us" not in written
    assert "is_us" not in _written_row(jobs_table, "k4")


async def test_a_write_failure_does_not_fail_the_poll(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BEST-EFFORT. The verdict re-derives from the same string on the next poll
    that touches the row, so a transient blip must not fail a poll whose upsert
    already succeeded — a failed poll counts toward the source's auto-disable
    threshold, and enough of them take a healthy board offline.

    Only THIS pass's write is broken, so a green ``summary["error"]`` cannot come
    from the cycle having skipped the write altogether."""
    real_write = poller_mod.poll_db_write
    attempts: list[str] = []

    async def _boom(supabase: Any, build: Any, *, label: str) -> Any:
        attempts.append(label)
        if label == "location is_us":
            raise RuntimeError("PostgREST exploded")
        return await real_write(supabase, build, label=label)

    monkeypatch.setattr(poller_mod, "poll_db_write", _boom)
    with caplog.at_level(logging.INFO, logger="app.services.poller"):
        _updates, _jobs_table, summary = await _run_poll(
            monkeypatch, [_location_job("Austin, TX")], upserted=[_upserted_row()]
        )

    assert "location is_us" in attempts, attempts  # precondition: it really tried
    assert summary["error"] is None
    assert summary["new"] == 1
    assert "location_us_marked=0" in _funnel_line(caplog)


async def test_the_pass_stamps_only_the_us_rows_of_a_mixed_batch() -> None:
    """Straight at the function, with a location the L1 admission gate would
    have dropped before the cycle-level tests could ever reach it: "Berlin,
    Germany" is as plainly non-US as a string gets, and it still gets nothing
    written — not ``False``, not anything."""
    supabase, jobs_table, _sources = _make_poll_supabase([])
    db = {"j1": {"archived_at": None, "is_us": None}, "j2": {"archived_at": None, "is_us": None}}
    updates = _record_updates(jobs_table, db)

    written = await poller_mod._apply_location_us_verdicts(
        supabase,
        [_location_job("Berlin, Germany"), _location_job("Denver, CO", external_id="k4")],
        [_upserted_row(), _upserted_row(row_id="j2", external_id="k4")],
    )

    assert written == 1
    assert [(u.payload, u.ids) for u in updates] == [({"is_us": True}, ["j2"])]
    assert db["j1"]["is_us"] is None


async def test_the_target_activation_path_stamps_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``_poll_one_source_for_target`` builds rows and upserts exactly like the
    scheduled cycle, so a pass wired into only one of them leaves the other
    ingesting untagged rows. Deleting the call there passes every other test."""
    with caplog.at_level(logging.INFO, logger="app.services.poller"):
        updates, jobs_table, summary = await _run_poll(
            monkeypatch,
            [_location_job("Austin, TX")],
            upserted=[_upserted_row()],
            target_path=True,
        )

    # Preconditions: this really is the OTHER path, and it really ingested.
    assert summary["error"] is None
    assert summary["new"] == 1
    assert jobs_table.upsert.called

    assert _payload_ids(updates) == [({"is_us": True}, ["j1"])]
    target_lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("poll_funnel_target ")
    ]
    assert any("location_us_marked=1" in line for line in target_lines), target_lines
