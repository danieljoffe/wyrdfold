"""The board's own country, applied at ingest (follow-up to lazy tagging).

Qualification tagging went lazy on 2026-08-26, so nothing stamps ``is_us`` at
ingest any more. A newly ingested listing carries ``is_us = NULL``, which every
read gate admits (``is_us IS NOT FALSE``) and which ``QUALIFICATION_ARCHIVE_NON_US``
never sees — 13.4% of newly tagged rows used to be archived at tag time, 133 of
134 of them non-US.

The poller's L1 ``is_us_location`` gate cannot take that job back. It drops a
listing only when the location string carries a known non-US hint AND no US
marker, so by construction every row it ADMITS is one the same parser cannot
call non-US. Ashby / Lever / SmartRecruiters publish the country as a
structured field; these tests pin that we read it, act on it, and never let it
be mistaken for silence.

Every "nothing was written" assertion runs a SECOND posting through the same
cycle whose board DID state a country, and asserts that one was written. A
no-write test that cannot tell "correctly silent" from "the feature is dead"
proves nothing; the sibling is what makes the silence meaningful.

Sample locations are ones the L1 gate really admits — asserted as a
precondition in ``test_board_metadata.py`` — so a gate change that swallowed
these rows would surface as a failing precondition rather than a green test.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings as live_settings
from app.services import poller as poller_mod
from app.services.qualification import materialize
from app.services.standard_job import StandardJob
from tests.test_poller import (
    _GUARD_SOURCE,
    _full_target,
    _make_poll_supabase,
    _make_targeted_poll_supabase,
)

pytestmark = pytest.mark.asyncio

# The control posting every no-write test rides alongside: an unambiguous
# board-stated non-US country on a location the L1 gate admits.
_CONTROL_ID = "control"
_CONTROL_ROW_ID = "job-control"


def _board_job(
    *,
    country: str | None,
    location: str = "Remote",
    external_id: str = "k3",
    title: str = "Staff Frontend Engineer",
) -> StandardJob:
    return StandardJob(
        external_id=external_id,
        title=title,
        location_name=location,
        content="",
        posted_at="2026-01-01",
        absolute_url=f"https://example.com/j/{external_id}",
        country=country,
    )


def _control_job() -> StandardJob:
    return _board_job(country="DE", external_id=_CONTROL_ID, title="Senior Frontend Developer")


def _upserted_row(*, row_id: str = "j1", external_id: str = "k3", **over: Any) -> dict[str, Any]:
    """A jobs-upsert RESULT row — the shape the post-upsert pass reads.

    ``is_us`` and ``archived_at`` come back as STORED (the payload never sets
    them), which is how the pass skips writes that would change nothing.
    """
    row: dict[str, Any] = {
        "id": row_id,
        "external_id": external_id,
        "title": "Staff Frontend Engineer",
        "company_name": "Acme",
        "location": "Remote",
        "description_html": "",
        "archived_at": None,
        "is_us": None,
        "qualified_hash": None,
        "qualified_at": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    row.update(over)
    return row


def _control_row() -> dict[str, Any]:
    return _upserted_row(row_id=_CONTROL_ROW_ID, external_id=_CONTROL_ID)


class _UpdateCall:
    """One recorded ``jobs.update(...)`` chain, WHOLE — and it APPLIES the WHERE.

    Two things a bare ``MagicMock`` gets wrong here.

    It records ``update()`` args and ``in_()`` args on separate mocks and drops
    the link between them, so an assertion built on it cannot see the
    ``.is_("archived_at", "null")`` filter at all and deleting that filter
    passes every test. This records the chain as one object.

    And it answers ``execute()`` with whatever you told it to, so a caller that
    counts the rows PostgREST actually changed can be tested against a fake
    that never changes anything. So this evaluates the recorded filters against
    ``db`` — the row state the WHERE clause would see — and returns exactly the
    rows it matched, which is what a real UPDATE returns (verified against the
    local stack: 3 ids in, one concurrently archived, 2 rows back).
    """

    def __init__(self, payload: dict[str, Any], db: dict[str, dict[str, Any]]) -> None:
        self.payload = payload
        self.ids: list[str] = []
        self.filters: list[tuple[str, Any]] = []
        self._db = db

    def in_(self, column: str, values: list[str]) -> _UpdateCall:
        assert column == "id", column
        self.ids = list(values)
        return self

    def is_(self, column: str, value: Any) -> _UpdateCall:
        self.filters.append((column, value))
        return self

    def _matches(self, stored: dict[str, Any]) -> bool:
        return all(
            stored.get(column) is None if value == "null" else stored.get(column) == value
            for column, value in self.filters
        )

    def execute(self) -> SimpleNamespace:
        matched: list[dict[str, Any]] = []
        for job_id in self.ids:
            stored = self._db.setdefault(job_id, {"archived_at": None})
            if not self._matches(stored):
                continue
            stored.update(self.payload)
            matched.append({"id": job_id, **stored})
        return SimpleNamespace(data=matched)


def _record_updates(jobs_table: MagicMock, db: dict[str, dict[str, Any]]) -> list[_UpdateCall]:
    calls: list[_UpdateCall] = []

    def _update(payload: dict[str, Any]) -> _UpdateCall:
        call = _UpdateCall(payload, db)
        calls.append(call)
        return call

    jobs_table.update.side_effect = _update
    return calls


async def _run_poll(
    monkeypatch: pytest.MonkeyPatch,
    jobs: list[StandardJob],
    *,
    upserted: list[dict[str, Any]],
    archive_non_us: bool = True,
    target_path: bool = False,
    db: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[_UpdateCall], MagicMock, dict[str, Any]]:
    """Drive a real poll cycle through ONE of the two ingest paths.

    ``target_path`` selects ``_poll_one_source_for_target`` (the activation
    fan-out) instead of ``_poll_one_source`` (the scheduled cycle). Both build
    rows the same way and both upsert; anything asserted here must hold on both
    or it is only half-pinned.
    """
    monkeypatch.setattr(live_settings, "qualification_enabled", True)
    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)
    monkeypatch.setattr(live_settings, "qualification_archive_non_us", archive_non_us)

    if target_path:
        supabase, jobs_table, _junction = _make_targeted_poll_supabase()
    else:
        supabase, jobs_table, _sources = _make_poll_supabase([])
    # Return only the rows THIS statement wrote. ``poll_db_upsert`` partitions a
    # heterogeneous batch by key-set (#931), so a fake that answers every call
    # with the whole list makes the poller count each row once per group — the
    # fixture would be modelling a single statement that no longer happens.
    _by_ext = {r.get("external_id"): r for r in upserted}

    def _upsert(payload: Any, **_kw: Any) -> MagicMock:
        rows = payload if isinstance(payload, list) else [payload]
        wrote = [_by_ext[e] for r in rows if (e := r.get("external_id")) in _by_ext]
        resp = MagicMock()
        resp.execute.return_value = MagicMock(data=wrote)
        return resp

    jobs_table.upsert.side_effect = _upsert
    # Default DB state agrees with the upsert snapshot; a test that wants the
    # snapshot-vs-DB race passes an explicit ``db``. ``is_us`` is carried as
    # well as ``archived_at`` because the location pass re-asserts
    # ``is_us IS NULL`` in its WHERE clause — a fixture that dropped the column
    # would let that filter match everything and could not fail.
    if db is None:
        db = {
            r["id"]: {"archived_at": r.get("archived_at"), "is_us": r.get("is_us")}
            for r in upserted
        }
    updates = _record_updates(jobs_table, db)

    async def _fetch(_token: str) -> list[StandardJob]:
        return list(jobs)

    async def _no_llm(*_a: Any, **_kw: Any) -> Any:  # ingest must stay $0 LLM
        raise AssertionError("ingest resolved an LLM client")

    monkeypatch.setattr(materialize, "get_llm_client_async", _no_llm)
    monkeypatch.setattr(poller_mod, "get_llm_client_async", _no_llm)
    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", _fetch)

    if target_path:
        summary = await poller_mod._poll_one_source_for_target(
            dict(_GUARD_SOURCE),
            supabase,
            _full_target(app_active=True, search_keywords=["frontend"]),
            payer_user_id="payer-1",
        )
        return updates, jobs_table, summary

    monkeypatch.setattr(
        poller_mod,
        "_active_targets",
        AsyncMock(return_value=[_full_target(app_active=True, search_keywords=["frontend"])]),
    )
    monkeypatch.setattr(
        poller_mod, "_resolve_user_targets_for_stage3", AsyncMock(return_value=({}, {}))
    )
    gate = MagicMock()
    gate.target_blocked.return_value = False
    gate.user_blocked.return_value = False

    summary = await poller_mod._poll_one_source(dict(_GUARD_SOURCE), supabase, budget_gate=gate)
    return updates, jobs_table, summary


def _payload_ids(updates: list[_UpdateCall]) -> list[tuple[dict[str, Any], list[str]]]:
    return [(u.payload, u.ids) for u in updates]


def _written_row(jobs_table: MagicMock, external_id: str) -> dict[str, Any]:
    rows = jobs_table.upsert.call_args[0][0]
    return next(r for r in rows if r["external_id"] == external_id)


def _assert_control_was_marked(updates: list[tuple[dict[str, Any], list[str]]]) -> None:
    """The mechanism is alive in THIS cycle — without this, a no-write
    assertion would pass just as happily against a feature that was deleted."""
    assert any(
        payload == {"is_us": False} and _CONTROL_ROW_ID in ids for payload, ids in updates
    ), updates


async def test_a_board_stated_non_us_country_marks_and_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pruning lazy tagging removed, bought back for free. Without this the
    row lands ``is_us = NULL``, which ``is_us IS NOT FALSE`` serves publicly."""
    updates, jobs_table, summary = await _run_poll(
        monkeypatch, [_board_job(country="DE")], upserted=[_upserted_row()]
    )

    # Preconditions: the row really was ingested, so the writes below mean
    # "the board's country was read", not "the poll did nothing".
    assert summary["error"] is None
    assert summary["new"] == 1
    assert jobs_table.upsert.called

    pairs = _payload_ids(updates)
    assert ({"is_us": False}, ["j1"]) in pairs
    assert any("archived_at" in payload and ids == ["j1"] for payload, ids in pairs)


async def test_a_silent_board_leaves_the_row_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Greenhouse and Workday publish no country. Silence is not falsity: an
    absent answer must never become ``is_us = False``, and must never archive."""
    updates, jobs_table, summary = await _run_poll(
        monkeypatch,
        [_board_job(country=None), _control_job()],
        upserted=[_upserted_row(), _control_row()],
    )

    assert summary["new"] == 2  # precondition: BOTH rows were ingested
    pairs = _payload_ids(updates)
    _assert_control_was_marked(pairs)
    assert all("j1" not in ids for _payload, ids in pairs), pairs


async def test_a_board_stated_us_country_is_recorded_and_not_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates, jobs_table, summary = await _run_poll(
        monkeypatch, [_board_job(country="US")], upserted=[_upserted_row()]
    )

    assert summary["new"] == 1
    assert _payload_ids(updates) == [({"is_us": True}, ["j1"])]


async def test_archiving_is_flag_gated_but_the_verdict_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A global-catalog self-host turns archiving off. The FACT still gets
    stored — it is the archive POLICY that is optional, and ``is_us = false``
    is what the read gates act on either way."""
    updates, jobs_table, _ = await _run_poll(
        monkeypatch,
        [_board_job(country="DE")],
        upserted=[_upserted_row()],
        archive_non_us=False,
    )

    assert _payload_ids(updates) == [({"is_us": False}, ["j1"])]


async def test_a_row_that_already_agrees_costs_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upsert RETURNING carries the STORED verdict, so a re-poll of an
    already-marked, already-archived row must not rewrite it — and must not
    move its ``archived_at`` forward."""
    updates, jobs_table, _ = await _run_poll(
        monkeypatch,
        [_board_job(country="DE"), _control_job()],
        upserted=[
            _upserted_row(is_us=False, archived_at="2026-01-02T00:00:00Z"),
            _control_row(),
        ],
    )

    pairs = _payload_ids(updates)
    _assert_control_was_marked(pairs)
    assert all("j1" not in ids for _payload, ids in pairs), pairs


async def test_a_plainly_us_location_vetoes_a_foreign_board_country(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-country posting whose postal address is abroad but which also
    lists a US office must not be pruned on the address alone (#60 workstream
    B).

    The assertion is about the PRUNE, not about the row being left untouched:
    the location pass that runs next reads the same "New York, NY" and stamps
    ``is_us = True``, which is the one direction that cannot hide a real job.
    What must never happen is the FALSE verdict or the archive — remove the
    ``positively_us_location`` veto from ``board_us_verdict`` and both appear.
    """
    updates, jobs_table, summary = await _run_poll(
        monkeypatch,
        [_board_job(country="GB", location="New York, NY; London"), _control_job()],
        upserted=[_upserted_row(), _control_row()],
    )

    assert summary["new"] == 2  # precondition: BOTH rows were ingested
    pairs = _payload_ids(updates)
    _assert_control_was_marked(pairs)
    assert not [
        (payload, ids)
        for payload, ids in pairs
        if "j1" in ids and (payload.get("is_us") is False or "archived_at" in payload)
    ], pairs


async def test_the_verdict_is_patched_into_the_rows_phase_2_will_grade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cycle_rows`` for this cycle's Phase-2 grading IS the upsert result.
    Leaving those dicts at their pre-update ``is_us = None`` would send a row we
    just archived straight to the grader — the waste this change removes."""
    row = _upserted_row()
    _u, _jobs_table, _ = await _run_poll(monkeypatch, [_board_job(country="DE")], upserted=[row])

    assert row["is_us"] is False
    assert row["archived_at"] is not None


async def test_the_display_country_lands_in_the_upsert_not_an_iso_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#805: ``jobs.country`` is read by filters that send ``UK``. The board's
    ``GB`` has to be translated on the way in, and it overrides the location
    parse — which for this string produced nothing at all."""
    _u, jobs_table, _ = await _run_poll(
        monkeypatch, [_board_job(country="GB")], upserted=[_upserted_row()]
    )

    assert _written_row(jobs_table, "k3")["country"] == "UK"


async def test_the_archive_write_re_asserts_that_the_row_is_still_unarchived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive UPDATE carries ``.is_("archived_at", "null")``.

    Without it, a row archived by url_health or the stale sweep BETWEEN the
    upsert and this write has its ``archived_at`` silently moved forward — the
    read-then-write race the WHERE clause closes. The ``is_us`` writes must NOT
    carry it: they are corrections that have to land on archived rows too."""
    updates, _jobs_table, _ = await _run_poll(
        monkeypatch, [_board_job(country="DE")], upserted=[_upserted_row()]
    )

    archive = [u for u in updates if "archived_at" in u.payload]
    assert len(archive) == 1, updates  # precondition: an archive WAS issued
    assert archive[0].filters == [("archived_at", "null")]
    verdicts = [u for u in updates if "is_us" in u.payload]
    assert verdicts and all(u.filters == [] for u in verdicts), verdicts


def _funnel_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("poll_funnel ")]
    assert lines, "precondition: the cycle emitted its funnel line"
    return lines[0]


async def test_the_counters_report_rows_written_not_rows_attempted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A row archived by another task BETWEEN the upsert snapshot and this write
    matches nothing, so it must not be counted as archived here.

    The write is already correct — ``.is_("archived_at", "null")`` sees to that.
    The counter is the thing at risk, and it is the only operational evidence
    for a mechanism that archives irreversibly: reporting attempts while being
    read as outcomes is how a partial write looks identical to a clean one.
    """
    row = _upserted_row()  # snapshot says live...
    with caplog.at_level(logging.INFO, logger="app.services.poller"):
        updates, _jobs_table, summary = await _run_poll(
            monkeypatch,
            [_board_job(country="DE")],
            upserted=[row],
            db={"j1": {"archived_at": "2026-01-02T00:00:00Z"}},  # ...the DB disagrees
        )

    # Preconditions: the row ingested, and the archive WAS attempted — otherwise
    # "archived=0" would be true for the boring reason.
    assert summary["new"] == 1
    attempted = [u for u in updates if "archived_at" in u.payload]
    assert len(attempted) == 1 and attempted[0].ids == ["j1"], updates

    assert "board_us_marked=1 board_us_archived=0" in _funnel_line(caplog)
    # ...and the row dict the grader reads is not told it was archived either.
    assert row["archived_at"] is None
    assert row["is_us"] is False  # the unfiltered verdict write DID land


async def test_the_counters_report_the_write_that_did_land(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The control for the test above: with no concurrent archive, both counters
    report 1. Without it, a counter hard-wired to zero would pass."""
    with caplog.at_level(logging.INFO, logger="app.services.poller"):
        await _run_poll(monkeypatch, [_board_job(country="DE")], upserted=[_upserted_row()])

    assert "board_us_marked=1 board_us_archived=1" in _funnel_line(caplog)


async def test_the_target_activation_path_prunes_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_poll_one_source_for_target`` builds rows and upserts exactly like the
    scheduled cycle, so a prune wired into only one of them leaves the other
    ingesting unpruned non-US rows. Deleting the call there passed every other
    test in the suite."""
    updates, jobs_table, summary = await _run_poll(
        monkeypatch,
        [_board_job(country="DE")],
        upserted=[_upserted_row()],
        target_path=True,
    )

    # Preconditions: this really is the OTHER path, and it really ingested.
    assert summary["error"] is None
    assert summary["new"] == 1
    assert jobs_table.upsert.called

    pairs = _payload_ids(updates)
    assert ({"is_us": False}, ["j1"]) in pairs
    assert any("archived_at" in payload and ids == ["j1"] for payload, ids in pairs)


async def test_the_target_activation_path_leaves_a_silent_board_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same silence contract on the second path, with the same live control."""
    updates, _jobs_table, summary = await _run_poll(
        monkeypatch,
        [_board_job(country=None), _control_job()],
        upserted=[_upserted_row(), _control_row()],
        target_path=True,
    )

    assert summary["new"] == 2  # precondition: BOTH rows were ingested
    pairs = _payload_ids(updates)
    _assert_control_was_marked(pairs)
    assert all("j1" not in ids for _payload, ids in pairs), pairs


async def test_an_unspellable_country_still_prunes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CH`` has no entry in the parser's display vocabulary, so the display
    column stays silent — but not being able to SPELL Switzerland has no
    bearing on whether the role is in the United States."""
    updates, jobs_table, _ = await _run_poll(
        monkeypatch, [_board_job(country="CH")], upserted=[_upserted_row()]
    )

    assert _written_row(jobs_table, "k3")["country"] is None
    assert ({"is_us": False}, ["j1"]) in _payload_ids(updates)
