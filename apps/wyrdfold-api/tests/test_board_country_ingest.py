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

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings as live_settings
from app.services import poller as poller_mod
from app.services.qualification import materialize
from app.services.standard_job import StandardJob
from tests.test_poller import _GUARD_SOURCE, _full_target, _make_poll_supabase

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


async def _run_poll(
    monkeypatch: pytest.MonkeyPatch,
    jobs: list[StandardJob],
    *,
    upserted: list[dict[str, Any]],
    archive_non_us: bool = True,
) -> tuple[MagicMock, dict[str, Any]]:
    """Drive a real ``_poll_one_source`` cycle and hand back the jobs table."""
    monkeypatch.setattr(live_settings, "qualification_enabled", True)
    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)
    monkeypatch.setattr(live_settings, "qualification_archive_non_us", archive_non_us)

    supabase, jobs_table, _sources = _make_poll_supabase([])
    jobs_table.upsert.return_value.execute.return_value.data = upserted

    async def _fetch(_token: str) -> list[StandardJob]:
        return list(jobs)

    async def _no_llm(*_a: Any, **_kw: Any) -> Any:  # ingest must stay $0 LLM
        raise AssertionError("ingest resolved an LLM client")

    monkeypatch.setattr(materialize, "get_llm_client_async", _no_llm)
    monkeypatch.setattr(poller_mod, "get_llm_client_async", _no_llm)
    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", _fetch)
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
    return jobs_table, summary


def _updates(jobs_table: MagicMock) -> list[tuple[dict[str, Any], list[str]]]:
    """Every ``jobs.update({...}).in_("id", [...])`` issued during the cycle."""
    payloads = [call.args[0] for call in jobs_table.update.call_args_list if call.args]
    id_lists = [list(call.args[1]) for call in jobs_table.update.return_value.in_.call_args_list]
    assert len(payloads) == len(id_lists), (payloads, id_lists)
    return list(zip(payloads, id_lists, strict=True))


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
    jobs_table, summary = await _run_poll(
        monkeypatch, [_board_job(country="DE")], upserted=[_upserted_row()]
    )

    # Preconditions: the row really was ingested, so the writes below mean
    # "the board's country was read", not "the poll did nothing".
    assert summary["error"] is None
    assert summary["new"] == 1
    assert jobs_table.upsert.called

    updates = _updates(jobs_table)
    assert ({"is_us": False}, ["j1"]) in updates
    assert any("archived_at" in payload and ids == ["j1"] for payload, ids in updates)


async def test_a_silent_board_leaves_the_row_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Greenhouse and Workday publish no country. Silence is not falsity: an
    absent answer must never become ``is_us = False``, and must never archive."""
    jobs_table, summary = await _run_poll(
        monkeypatch,
        [_board_job(country=None), _control_job()],
        upserted=[_upserted_row(), _control_row()],
    )

    assert summary["new"] == 2  # precondition: BOTH rows were ingested
    updates = _updates(jobs_table)
    _assert_control_was_marked(updates)
    assert all("j1" not in ids for _payload, ids in updates), updates


async def test_a_board_stated_us_country_is_recorded_and_not_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_table, summary = await _run_poll(
        monkeypatch, [_board_job(country="US")], upserted=[_upserted_row()]
    )

    assert summary["new"] == 1
    assert _updates(jobs_table) == [({"is_us": True}, ["j1"])]


async def test_archiving_is_flag_gated_but_the_verdict_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A global-catalog self-host turns archiving off. The FACT still gets
    stored — it is the archive POLICY that is optional, and ``is_us = false``
    is what the read gates act on either way."""
    jobs_table, _ = await _run_poll(
        monkeypatch,
        [_board_job(country="DE")],
        upserted=[_upserted_row()],
        archive_non_us=False,
    )

    assert _updates(jobs_table) == [({"is_us": False}, ["j1"])]


async def test_a_row_that_already_agrees_costs_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upsert RETURNING carries the STORED verdict, so a re-poll of an
    already-marked, already-archived row must not rewrite it — and must not
    move its ``archived_at`` forward."""
    jobs_table, _ = await _run_poll(
        monkeypatch,
        [_board_job(country="DE"), _control_job()],
        upserted=[
            _upserted_row(is_us=False, archived_at="2026-01-02T00:00:00Z"),
            _control_row(),
        ],
    )

    updates = _updates(jobs_table)
    _assert_control_was_marked(updates)
    assert all("j1" not in ids for _payload, ids in updates), updates


async def test_a_plainly_us_location_vetoes_a_foreign_board_country(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-country posting whose postal address is abroad but which also
    lists a US office must not be pruned on the address alone (#60 workstream
    B). Leaving it NULL hands the decision to the grader."""
    jobs_table, summary = await _run_poll(
        monkeypatch,
        [_board_job(country="GB", location="New York, NY; London"), _control_job()],
        upserted=[_upserted_row(), _control_row()],
    )

    assert summary["new"] == 2  # precondition: BOTH rows were ingested
    updates = _updates(jobs_table)
    _assert_control_was_marked(updates)
    assert all("j1" not in ids for _payload, ids in updates), updates


async def test_the_verdict_is_patched_into_the_rows_phase_2_will_grade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cycle_rows`` for this cycle's Phase-2 grading IS the upsert result.
    Leaving those dicts at their pre-update ``is_us = None`` would send a row we
    just archived straight to the grader — the waste this change removes."""
    row = _upserted_row()
    _jobs_table, _ = await _run_poll(monkeypatch, [_board_job(country="DE")], upserted=[row])

    assert row["is_us"] is False
    assert row["archived_at"] is not None


async def test_the_display_country_lands_in_the_upsert_not_an_iso_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#805: ``jobs.country`` is read by filters that send ``UK``. The board's
    ``GB`` has to be translated on the way in, and it overrides the location
    parse — which for this string produced nothing at all."""
    jobs_table, _ = await _run_poll(
        monkeypatch, [_board_job(country="GB")], upserted=[_upserted_row()]
    )

    assert _written_row(jobs_table, "k3")["country"] == "UK"


async def test_an_unspellable_country_still_prunes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CH`` has no entry in the parser's display vocabulary, so the display
    column stays silent — but not being able to SPELL Switzerland has no
    bearing on whether the role is in the United States."""
    jobs_table, _ = await _run_poll(
        monkeypatch, [_board_job(country="CH")], upserted=[_upserted_row()]
    )

    assert _written_row(jobs_table, "k3")["country"] is None
    assert ({"is_us": False}, ["j1"]) in _updates(jobs_table)
