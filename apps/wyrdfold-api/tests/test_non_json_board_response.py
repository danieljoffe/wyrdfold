"""A board that answers 200 with a non-JSON body must fail cleanly.

Every ATS fetcher called ``resp.json()`` bare on a 200. That holds until a board
serves an HTML interstitial, a WAF challenge, a maintenance page or an empty
body with a 200 — and then the poll dies with

    json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

raised six frames deep inside httpx. Prod logged that repeatedly against
Workday boards (``Poll failed for Adams``), where it reads as a code bug in the
traceback when it is really an upstream serving HTML.

The contract matches the non-200 case each fetcher handles, and it splits by
which endpoint answered:

* a **LIST** response that won't parse is a FAILED FETCH — the fetcher raises
  ``BoardFetchError`` so the poller counts it toward the disable threshold. A
  WAF challenge page is not "this board has no open roles", and treating it as
  one is what let a broken board reset its own failure counter every cycle
  (see tests/test_dead_board_failure_accounting.py).
* a **DETAIL** response that won't parse still warns and drops just that one
  posting — one bad row must not fail the whole board.

Either way the fetcher yields NO partial harvest. That part is load-bearing:
the poller skips its stale-archive pass for a source that returns zero rows
while active rows exist, so a failed fetch cannot archive live listings. A
partial result would sail straight past that guard. Raising keeps the same
property by never reaching the guard at all.

What must NOT come back is the raw ``JSONDecodeError`` from six frames inside
httpx — that reads as a code bug when it is really an upstream serving HTML.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from app.http_client import BoardFetchError, json_or_none
from app.services import ashby, greenhouse, lever, smartrecruiters, workday

# The real shape: a WAF/interstitial page served with 200 and text/html.
_HTML_BODY = (
    "<!DOCTYPE html><html><head><title>Attention Required!</title></head>"
    "<body><h1>Sorry, you have been blocked</h1></body></html>"
)


def _html_response(status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": "text/html; charset=UTF-8"},
        text=_HTML_BODY,
        request=httpx.Request("GET", "https://boards.example.com/jobs"),
    )


def test_json_or_none_returns_none_and_warns_on_html(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert json_or_none(_html_response(), source="workday acme") is None
    assert any("non-JSON body" in r.message for r in caplog.records)


def test_json_or_none_does_not_log_the_whole_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A block page can be tens of KB; the log keeps a 120-char sample."""
    big = httpx.Response(
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html>" + ("x" * 50_000) + "</html>",
        request=httpx.Request("GET", "https://boards.example.com/jobs"),
    )
    with caplog.at_level(logging.WARNING):
        assert json_or_none(big, source="workday acme") is None
    assert all(len(r.getMessage()) < 500 for r in caplog.records)


def test_json_or_none_passes_valid_json_through() -> None:
    ok = httpx.Response(
        status_code=200,
        json={"jobs": [{"id": 1}]},
        request=httpx.Request("GET", "https://boards.example.com/jobs"),
    )
    assert json_or_none(ok, source="greenhouse acme") == {"jobs": [{"id": 1}]}


@pytest.mark.parametrize(
    ("module", "func_name", "arg"),
    [
        (greenhouse, "fetch_board_jobs", "acme"),
        (lever, "fetch_lever_jobs", "acme"),
        (ashby, "fetch_ashby_jobs", "acme"),
        (workday, "fetch_workday_jobs", "https://acme.wd1.myworkdayjobs.com|acme|External"),
    ],
)
async def test_fetcher_raises_a_typed_error_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, module: Any, func_name: str, arg: str
) -> None:
    """THE original regression: this used to raise JSONDecodeError out of the
    fetcher and kill the whole poll for that source. It now raises a typed
    ``BoardFetchError`` the poller counts — and, either way, never a bare
    ``JSONDecodeError`` and never a partial harvest."""

    async def _fake_request(*_a: Any, **_kw: Any) -> httpx.Response:
        return _html_response()

    monkeypatch.setattr(module, "request_with_retry", _fake_request)
    with pytest.raises(BoardFetchError) as exc_info:
        await getattr(module, func_name)(arg)
    assert not isinstance(exc_info.value, ValueError)  # not a JSONDecodeError
    assert exc_info.value.status == 200


async def test_smartrecruiters_list_raises_on_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_request(*_a: Any, **_kw: Any) -> httpx.Response:
        return _html_response()

    monkeypatch.setattr(smartrecruiters, "request_with_retry", _fake_request)
    with pytest.raises(BoardFetchError):
        await smartrecruiters.fetch_smartrecruiters_jobs("acme")


async def test_detail_fetch_still_drops_just_that_posting_on_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTROL for the split: the per-posting DETAIL path is deliberately
    best-effort and must NOT raise — one unparseable posting drops out and the
    rest of the board still ingests."""
    calls: list[str] = []

    async def _fake_request(_method: str, url: str, **_kw: Any) -> httpx.Response:
        calls.append(url)
        if url.endswith("/postings"):
            return httpx.Response(
                status_code=200,
                json={"content": [{"id": "sr-1"}, {"id": "sr-2"}]},
                request=httpx.Request("GET", url),
            )
        if url.endswith("sr-1"):
            return _html_response()  # one bad detail
        return httpx.Response(
            status_code=200,
            json={
                "id": "sr-2",
                "name": "Senior Engineer",
                "jobAd": {"sections": {"jobDescription": {"text": "<p>Build.</p>"}}},
                "postingUrl": "https://jobs.example.com/sr-2",
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(smartrecruiters, "request_with_retry", _fake_request)
    jobs = await smartrecruiters.fetch_smartrecruiters_jobs("acme")

    # Precondition: both details really were attempted.
    assert sum(1 for u in calls if "/postings/" in u) == 2
    assert [j.external_id for j in jobs] == ["sr-2"]
