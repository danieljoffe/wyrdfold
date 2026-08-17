"""Workday: don't refetch the detail for postings we already hold unchanged.

Workday is the ONLY provider needing a request per posting — everyone else
returns content in the list call. Refetching all of them every cycle cost
**53,191 detail requests per cycle** across 1,108 enabled boards, all queued
behind ``_POD_CONCURRENCY = 4`` per pod (wd1 alone: 455 boards, 21,649
postings). That is roughly an hour of aggregate queue time, and it is what
starved the 300s per-source budget: six boards were cancelled in one 16h prod
window, and ``Ensemblehp`` had not produced a candidate in seven weeks.

Two hazards this must not trip, both covered below:

- a skipped posting is still RETURNED, or the stale-archive pass would treat
  it as delisted and archive a live listing;
- a skipped posting must NOT build an upsert row, because its ``content`` is
  empty and writing it would blank the stored description.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.services import workday
from app.services.date_normalize import normalize_posted_at
from app.services.workday import KnownPosting, _needs_detail, _posting_bucket, _refresh_bucket

_SHALLOW = [
    {"externalPath": "/job/a", "title": "Engineer", "postedOn": "Posted Today",
     "locationsText": "Austin, TX"},
    {"externalPath": "/job/b", "title": "Designer", "postedOn": "Posted Today",
     "locationsText": "Austin, TX"},
]


def _stored_today() -> str:
    """What ``jobs.source_posted_at`` actually holds for a posting whose list
    entry says "Posted Today" — the NORMALIZED form, which is what the DB
    returns. Fixtures used the raw string on both sides, so they matched by
    construction and missed that the real comparison never matched at all."""
    return normalize_posted_at("Posted Today") or ""


def _known(**over: Any) -> dict[str, KnownPosting]:
    base = {
        "/job/a": KnownPosting(title="Engineer", posted_at_stored=_stored_today()),
        "/job/b": KnownPosting(title="Designer", posted_at_stored=_stored_today()),
    }
    base.update(over)
    return base


# ---- the decision ----------------------------------------------------------


def test_unseen_posting_is_always_fetched() -> None:
    assert _needs_detail("/job/new", _SHALLOW[0], _known(), due_bucket=-1) is True


def test_unchanged_posting_is_skipped() -> None:
    assert _needs_detail("/job/a", _SHALLOW[0], _known(), due_bucket=-1) is False


def test_retitled_posting_is_fetched() -> None:
    item = {**_SHALLOW[0], "title": "Senior Engineer"}
    assert _needs_detail("/job/a", item, _known(), due_bucket=-1) is True


def test_reposted_posting_is_fetched() -> None:
    item = {**_SHALLOW[0], "postedOn": "Posted 3 Days Ago"}
    assert _needs_detail("/job/a", item, _known(), due_bucket=-1) is True


def test_no_known_map_fetches_everything() -> None:
    """A source with no prior rows — or a failed pre-read — behaves as before."""
    assert _needs_detail("/job/a", _SHALLOW[0], None, due_bucket=-1) is True
    assert _needs_detail("/job/a", _SHALLOW[0], {}, due_bucket=-1) is True


def test_rolling_refresh_fetches_an_unchanged_posting_when_its_bucket_is_due() -> None:
    """A description edited without a postedOn bump would otherwise never be
    re-read. Every posting's bucket comes due within _REFRESH_BUCKETS hours."""
    due = _posting_bucket("/job/a")
    assert _needs_detail("/job/a", _SHALLOW[0], _known(), due_bucket=due) is True


def test_every_posting_is_refreshed_within_the_bucket_window() -> None:
    """No posting can hide from the rolling refresh forever."""
    buckets = {_posting_bucket(f"/job/{n}") for n in range(500)}
    assert buckets <= set(range(workday._REFRESH_BUCKETS))
    hours = {_refresh_bucket(datetime(2026, 8, 17, h, tzinfo=UTC)) for h in range(24)}
    assert len(hours) >= min(24, workday._REFRESH_BUCKETS) - 1


# ---- the fetch ------------------------------------------------------------


def _list_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"jobPostings": _SHALLOW, "total": len(_SHALLOW)},
        request=httpx.Request("POST", "https://acme.wd1.myworkdayjobs.com/x"),
    )


async def test_skipped_postings_are_returned_but_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE archive hazard: a skipped posting must still come back, or the
    stale-archive pass deletes a live listing."""
    detail_calls: list[str] = []

    async def _fake_request(*_a: Any, **_kw: Any) -> httpx.Response:
        return _list_response()

    async def _fake_detail(*, external_path: str, **_kw: Any) -> dict[str, Any]:
        detail_calls.append(external_path)
        return {"title": "Engineer", "jobDescription": "<p>hi</p>", "postedOn": "Posted Today"}

    monkeypatch.setattr(workday, "request_with_retry", _fake_request)
    monkeypatch.setattr(workday, "_fetch_one_posting_detail", _fake_detail)
    monkeypatch.setattr(workday, "_refresh_bucket", lambda *_a, **_k: -1)

    jobs = await workday.fetch_workday_jobs(
        "https://acme.wd1.myworkdayjobs.com|acme|External", known=_known()
    )

    assert detail_calls == [], "no detail should have been fetched"
    assert {j.external_id for j in jobs} == {"/job/a", "/job/b"}
    assert all(j.detail_skipped for j in jobs)


async def test_only_the_changed_posting_is_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail_calls: list[str] = []

    async def _fake_request(*_a: Any, **_kw: Any) -> httpx.Response:
        return _list_response()

    async def _fake_detail(*, external_path: str, **_kw: Any) -> dict[str, Any]:
        detail_calls.append(external_path)
        return {"title": "Designer II", "jobDescription": "<p>hi</p>", "postedOn": "Posted Today"}

    monkeypatch.setattr(workday, "request_with_retry", _fake_request)
    monkeypatch.setattr(workday, "_fetch_one_posting_detail", _fake_detail)
    monkeypatch.setattr(workday, "_refresh_bucket", lambda *_a, **_k: -1)

    known = _known(**{"/job/b": KnownPosting(title="Designer II", posted_at_stored=_stored_today())})
    jobs = await workday.fetch_workday_jobs(
        "https://acme.wd1.myworkdayjobs.com|acme|External", known=known
    )

    assert detail_calls == ["/job/b"], detail_calls
    by_id = {j.external_id: j for j in jobs}
    assert by_id["/job/a"].detail_skipped is True
    assert by_id["/job/b"].detail_skipped is False
    assert by_id["/job/b"].content == "<p>hi</p>"


async def test_without_known_every_detail_is_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-read failing, or a brand-new source, must not change behaviour."""
    detail_calls: list[str] = []

    async def _fake_request(*_a: Any, **_kw: Any) -> httpx.Response:
        return _list_response()

    async def _fake_detail(*, external_path: str, **_kw: Any) -> dict[str, Any]:
        detail_calls.append(external_path)
        return {"title": "Engineer", "jobDescription": "<p>hi</p>", "postedOn": "Posted Today"}

    monkeypatch.setattr(workday, "request_with_retry", _fake_request)
    monkeypatch.setattr(workday, "_fetch_one_posting_detail", _fake_detail)

    jobs = await workday.fetch_workday_jobs("https://acme.wd1.myworkdayjobs.com|acme|External")

    assert sorted(detail_calls) == ["/job/a", "/job/b"]
    assert not any(j.detail_skipped for j in jobs)


# ---- rejecting on the LIST entry, before spending a request -----------------
#
# The dominant waste was never the postings we hold — we hold only 5.8% of what
# Workday lists (3,092 live rows against 53,191 postings per cycle). It was the
# other 94%: postings the poller drops on its two FREE gates (title matches no
# active target, or the role isn't US-based). Both fields are already in the
# list entry, so their descriptions were fetched purely to be thrown away.


async def _fetch_with(monkeypatch, **kwargs: Any) -> tuple[list[Any], list[str]]:
    detail_calls: list[str] = []

    async def _fake_request(*_a: Any, **_kw: Any) -> httpx.Response:
        return _list_response()

    async def _fake_detail(*, external_path: str, **_kw: Any) -> dict[str, Any]:
        detail_calls.append(external_path)
        return {"title": "Engineer", "jobDescription": "<p>hi</p>", "postedOn": "Posted Today"}

    monkeypatch.setattr(workday, "request_with_retry", _fake_request)
    monkeypatch.setattr(workday, "_fetch_one_posting_detail", _fake_detail)
    monkeypatch.setattr(workday, "_refresh_bucket", lambda *_a, **_k: -1)
    jobs = await workday.fetch_workday_jobs(
        "https://acme.wd1.myworkdayjobs.com|acme|External", **kwargs
    )
    return jobs, detail_calls


async def test_postings_the_free_gates_reject_cost_no_detail_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, detail_calls = await _fetch_with(
        monkeypatch, admissible=lambda _title, _loc: False
    )
    assert detail_calls == [], "a rejected posting must not cost a request"
    # Still returned, so the stale-archive pass counts them as seen — the
    # poller drops them at the same gates it always did.
    assert {j.external_id for j in jobs} == {"/job/a", "/job/b"}
    assert all(j.detail_skipped for j in jobs)


async def test_only_admissible_postings_are_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs, detail_calls = await _fetch_with(
        monkeypatch, admissible=lambda title, _loc: title == "Designer"
    )
    assert detail_calls == ["/job/b"], detail_calls
    by_id = {j.external_id: j for j in jobs}
    assert by_id["/job/a"].detail_skipped is True
    assert by_id["/job/b"].detail_skipped is False


async def test_no_predicate_means_no_opinion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers that don't pass one keep today's behaviour exactly."""
    _jobs, detail_calls = await _fetch_with(monkeypatch)
    assert sorted(detail_calls) == ["/job/a", "/job/b"]


async def test_the_gates_see_the_list_entrys_title_and_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned because the predicate is the poller's free gates: if the fetcher
    passed the wrong fields, every posting would be judged on empty strings and
    silently dropped."""
    seen: list[tuple[str, str | None]] = []
    await _fetch_with(
        monkeypatch,
        admissible=lambda title, loc: seen.append((title, loc)) is None,
    )
    assert seen == [("Engineer", "Austin, TX"), ("Designer", "Austin, TX")]
