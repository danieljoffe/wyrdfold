"""A board that answers 200 with a non-JSON body must fail cleanly.

Every ATS fetcher called ``resp.json()`` bare on a 200. That holds until a board
serves an HTML interstitial, a WAF challenge, a maintenance page or an empty
body with a 200 — and then the poll dies with

    json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

raised six frames deep inside httpx. Prod logged that repeatedly against
Workday boards (``Poll failed for Adams``), where it reads as a code bug in the
traceback when it is really an upstream serving HTML.

The contract now matches the non-200 case each fetcher already handled: warn,
and yield NO jobs. Yielding nothing rather than a partial harvest is
load-bearing — the poller skips its stale-archive pass for a source that
returns zero rows while active rows exist, so a failed fetch cannot archive
live listings. A partial result would sail straight past that guard.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from app.http_client import json_or_none
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
async def test_fetcher_yields_no_jobs_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, module: Any, func_name: str, arg: str
) -> None:
    """THE regression: this used to raise JSONDecodeError out of the fetcher
    and kill the whole poll for that source."""

    async def _fake_request(*_a: Any, **_kw: Any) -> httpx.Response:
        return _html_response()

    monkeypatch.setattr(module, "request_with_retry", _fake_request)
    jobs = await getattr(module, func_name)(arg)
    assert jobs == []


async def test_smartrecruiters_list_yields_no_jobs_on_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_request(*_a: Any, **_kw: Any) -> httpx.Response:
        return _html_response()

    monkeypatch.setattr(smartrecruiters, "request_with_retry", _fake_request)
    assert await smartrecruiters.fetch_smartrecruiters_jobs("acme") == []
