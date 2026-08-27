"""A dead board must COUNT as a failed poll; an empty board must not.

Prod, before this: comcast's Workday board answered ``410 Gone`` every cycle
and the source stayed enabled forever.

    WARNING [app.services.workday] workday https://comcast.wd5.myworkdayjobs.com|
        comcast|Comcast_Careers returned 410 at offset 0
    WARNING [app.services.poller] poll Comcast returned 0 jobs but 4 active rows
        exist — skipping stale-archive pass (suspected fetch failure)

Every ATS list fetcher collapsed a non-200 into ``return []``, which is
indistinguishable from "this board has no open roles". The poller therefore
recorded a SUCCESSFUL poll and wrote ``consecutive_failures: 0`` — the dead
board RESET its own failure counter every cycle, so
``source_failure_disable_threshold`` could never fire for this failure class
and ``_record_source_failure`` (reachable only from an exception handler) was
never called.

These tests drive the REAL fetchers through ``_poll_one_source`` against a
mocked HTTP client, so they exercise the actual swallow-vs-raise decision
rather than a stand-in fetcher.

The control cases are the load-bearing half: a 200 carrying zero postings is a
legitimately empty board and must still reset the counter. Each control
asserts its precondition (the poll really did run to completion) before
asserting the counter, so it cannot pass because the fetch blew up early.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings as live_settings
from app.http_client import BoardFetchError

pytestmark = pytest.mark.asyncio


# ---- provider matrix --------------------------------------------------------
#
# Two transports and three URL shapes, because the fix is cross-cutting: a
# per-provider patch would pass one row and fail the others.

_GREENHOUSE = {
    "id": "src-gh",
    "board_token": "acme",
    "provider": "greenhouse",
    "company_name": "Acme",
}
_ASHBY = {
    "id": "src-ashby",
    "board_token": "acme",
    "provider": "ashby",
    "company_name": "Acme",
}
_WORKDAY = {
    "id": "src-wd",
    "board_token": "https://comcast.wd5.myworkdayjobs.com|comcast|Comcast_Careers",
    "provider": "workday",
    "company_name": "Comcast",
}

# (source, HTTP verb the fetcher uses, body a 200 with zero postings returns)
_PROVIDERS = [
    pytest.param(_GREENHOUSE, "get", {"jobs": []}, id="greenhouse"),
    pytest.param(_ASHBY, "get", {"jobs": []}, id="ashby"),
    pytest.param(_WORKDAY, "post", {"total": 0, "jobPostings": []}, id="workday"),
]


def _resp(status: int, body: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.json.return_value = body if body is not None else {}
    resp.text = ""
    return resp


def _wire(mock_http_client: MagicMock, verb: str, resp: MagicMock) -> None:
    """Answer the fetcher's list request with ``resp``, whichever verb it uses."""
    setattr(mock_http_client, verb, AsyncMock(return_value=resp))


def _poll_supabase(existing_rows: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    from tests.test_poller import _make_poll_supabase

    supabase, _jobs_table, sources_table = _make_poll_supabase(existing_rows)
    return supabase, sources_table


def _sources_update_payload(sources_table: MagicMock) -> dict[str, Any]:
    """The single UPDATE this poll wrote to ``sources``."""
    assert sources_table.update.call_count == 1, (
        f"expected exactly one sources UPDATE, got {sources_table.update.call_args_list}"
    )
    payload = sources_table.update.call_args.args[0]
    assert isinstance(payload, dict)
    return payload


# Four active rows, mirroring the prod Comcast row count the stale-archive
# guard was protecting.
_EXISTING = [
    {"id": f"job-{i}", "external_id": f"e-{i}", "title": f"T{i}", "company_name": "Acme"}
    for i in range(1, 5)
]


# ---- a non-200 list response is a FAILURE ----------------------------------


@pytest.mark.parametrize(("source", "verb", "_empty_body"), _PROVIDERS)
@pytest.mark.parametrize("status", [410, 404, 422, 403])
async def test_non_200_list_increments_failures_and_never_resets(
    mock_http_client: MagicMock,
    source: dict[str, Any],
    verb: str,
    _empty_body: Any,
    status: int,
) -> None:
    """410 (the prod case) plus the other statuses the fetchers swallowed.

    Before the fix every one of these wrote ``consecutive_failures: 0``.
    """
    from app.services import poller as poller_mod

    _wire(mock_http_client, verb, _resp(status))
    supabase, sources_table = _poll_supabase(_EXISTING)

    row = {**source, "consecutive_failures": 3}
    summary = await poller_mod._poll_one_source(row, supabase, active_targets=[])

    # THE regression, asserted first so a failure names it directly: the old
    # code wrote ``consecutive_failures: 0`` here on every cycle.
    payload = _sources_update_payload(sources_table)
    assert payload["consecutive_failures"] == 4, (
        "a dead board must count toward the disable threshold, "
        f"got {payload.get('consecutive_failures')!r}"
    )
    assert "job_count" not in payload, "a failed fetch is not a successful poll"
    assert payload["last_error"]
    assert str(status) in payload["last_error"]
    # Below the threshold — counted, not yet retired.
    assert "enabled" not in payload

    # …and the poll reports itself as failed rather than as a clean cycle.
    assert summary["polled"] is False
    assert summary["error"] is not None


@pytest.mark.parametrize(("source", "verb", "_empty_body"), _PROVIDERS)
async def test_transport_failure_past_its_retries_counts_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    mock_http_client: MagicMock,
    source: dict[str, Any],
    verb: str,
    _empty_body: Any,
) -> None:
    """A 5xx/429 is retried by ``request_with_retry`` first; only once the
    retries are spent does it reach the failure counter."""
    import httpx

    from app.services import poller as poller_mod

    monkeypatch.setattr("app.http_client._sleep", AsyncMock())
    setattr(mock_http_client, verb, AsyncMock(side_effect=httpx.ConnectError("boom")))
    supabase, sources_table = _poll_supabase(_EXISTING)

    row = {**source, "consecutive_failures": 0}
    summary = await poller_mod._poll_one_source(row, supabase, active_targets=[])

    payload = _sources_update_payload(sources_table)
    assert payload["consecutive_failures"] == 1, (
        f"an unreachable board must count, got {payload.get('consecutive_failures')!r}"
    )
    assert summary["polled"] is False


@pytest.mark.parametrize(("source", "verb", "_empty_body"), _PROVIDERS)
async def test_failed_poll_rotates_to_the_back_of_the_due_queue(
    mock_http_client: MagicMock,
    source: dict[str, Any],
    verb: str,
    _empty_body: Any,
) -> None:
    """The interaction that makes counting failures safe at fleet scale.

    ``poll_due_sources`` polls the ``poll_max_sources_per_cycle`` MOST OVERDUE
    sources, ordered by ``last_polled_at`` — so a source that fails without
    being stamped pins itself to the front of that queue and re-hogs a slot on
    every tick, crowding out healthy sources. Roughly 95 prod boards start
    failing the moment this ships, which is more than a third of a 250-source
    tick, so this is load-bearing rather than tidy.
    """
    from app.services import poller as poller_mod

    _wire(mock_http_client, verb, _resp(410))
    supabase, sources_table = _poll_supabase(_EXISTING)

    await poller_mod._poll_one_source(
        {**source, "consecutive_failures": 0}, supabase, active_targets=[]
    )

    payload = _sources_update_payload(sources_table)
    # Precondition: this really is the failure write, not a mark-polled write.
    assert payload["consecutive_failures"] == 1
    assert payload["last_polled_at"], "a failed source must rotate to the queue's back"
    # …but it must NOT masquerade as a clean poll.
    assert "job_count" not in payload
    assert payload["consecutive_failures"] != 0


@pytest.mark.parametrize(("source", "verb", "_empty_body"), _PROVIDERS)
async def test_reaching_the_threshold_disables_the_source(
    mock_http_client: MagicMock,
    source: dict[str, Any],
    verb: str,
    _empty_body: Any,
) -> None:
    """The whole point: the existing auto-disable machinery can finally fire."""
    from app.services import poller as poller_mod

    _wire(mock_http_client, verb, _resp(410))
    supabase, sources_table = _poll_supabase(_EXISTING)

    threshold = live_settings.source_failure_disable_threshold
    assert threshold > 0, "backoff must be armed for this test to mean anything"

    row = {**source, "consecutive_failures": threshold - 1}
    await poller_mod._poll_one_source(row, supabase, active_targets=[])

    payload = _sources_update_payload(sources_table)
    assert payload["consecutive_failures"] == threshold
    assert payload["enabled"] is False
    assert payload["disabled_at"]


# ---- CONTROL: a 200 with zero postings is a real, empty board ---------------


@pytest.mark.parametrize(("source", "verb", "empty_body"), _PROVIDERS)
async def test_empty_board_is_not_a_failure_and_resets_the_counter(
    mock_http_client: MagicMock,
    source: dict[str, Any],
    verb: str,
    empty_body: Any,
) -> None:
    """The anti-vacuous half. A board that answers 200 with no postings is
    healthy — it must NOT be counted, and it must still clear a counter left
    over from an earlier outage.

    Preconditions are asserted first: if the fetch had blown up early, the
    "not counted" assertion below would pass for the wrong reason.
    """
    from app.services import poller as poller_mod

    _wire(mock_http_client, verb, _resp(200, empty_body))
    # No existing rows, so the stale-archive pass is a no-op either way and
    # this test is only about the failure accounting.
    supabase, sources_table = _poll_supabase([])

    row = {**source, "consecutive_failures": 7}
    summary = await poller_mod._poll_one_source(row, supabase, active_targets=[])

    # PRECONDITIONS — the poll ran to completion on the success path.
    assert summary["polled"] is True, "the fetch must have succeeded"
    assert summary["error"] is None
    assert summary["new"] == 0

    payload = _sources_update_payload(sources_table)
    assert "last_polled_at" in payload, "this must be the mark-polled write"
    assert payload["job_count"] == 0
    assert payload["consecutive_failures"] == 0, "an empty board is not a failure"
    assert payload["last_error"] is None
    assert "enabled" not in payload, "an empty board must never be auto-disabled"


@pytest.mark.parametrize(("source", "verb", "empty_body"), _PROVIDERS)
async def test_empty_board_with_active_rows_still_skips_the_stale_pass(
    mock_http_client: MagicMock,
    source: dict[str, Any],
    verb: str,
    empty_body: Any,
) -> None:
    """The stale-archive guard is CORRECT and stays. A legitimately empty
    board with live rows still declines to archive them (they age out via
    recency instead) — the fix must not have turned that into a delisting."""
    from app.services import poller as poller_mod

    _wire(mock_http_client, verb, _resp(200, empty_body))
    supabase, sources_table = _poll_supabase(_EXISTING)

    summary = await poller_mod._poll_one_source(
        {**source, "consecutive_failures": 0}, supabase, active_targets=[]
    )

    assert summary["polled"] is True
    assert summary["archived"] == 0
    assert _sources_update_payload(sources_table)["consecutive_failures"] == 0


# ---- the fetchers raise a typed error, not a bare exception ------------------


async def test_fetcher_error_is_boardfetcherror_carrying_the_status(
    mock_http_client: MagicMock,
) -> None:
    """The poller's quiet-log branch keys off the type, so the type matters."""
    from app.services.greenhouse import fetch_board_jobs

    mock_http_client.get = AsyncMock(return_value=_resp(410))
    with pytest.raises(BoardFetchError) as exc_info:
        await fetch_board_jobs("acme")
    assert exc_info.value.status == 410
    assert "acme" in exc_info.value.source


async def test_board_fetch_failure_logs_a_warning_not_a_traceback(
    mock_http_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """95 dead boards must not each print a traceback that reads like a code
    bug. An upstream board refusing to serve is operational weather."""
    import logging

    from app.services import poller as poller_mod

    mock_http_client.get = AsyncMock(return_value=_resp(410))
    supabase, _sources_table = _poll_supabase(_EXISTING)

    with caplog.at_level(logging.WARNING, logger="app.services.poller"):
        await poller_mod._poll_one_source(
            {**_GREENHOUSE, "consecutive_failures": 0}, supabase, active_targets=[]
        )

    poll_records = [r for r in caplog.records if r.name == "app.services.poller"]
    named = [r for r in poll_records if "410" in r.getMessage()]
    assert named, (
        "the poller must log the board's refusal (and the status), "
        f"got {[r.getMessage() for r in poll_records]}"
    )
    assert all(r.exc_info is None for r in poll_records), (
        "a dead board should not print a traceback"
    )
