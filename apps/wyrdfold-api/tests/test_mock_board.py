"""Synthetic mock board provider (#57 load-test rig).

Two properties matter: it must be IMPOSSIBLE to trigger in a default
(prod-shaped) config, and the feed must be deterministic so flag-off vs.
flag-on load runs upsert identical rows.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.mock_board import _MAX_JOB_COUNT, fetch_mock_jobs
from app.services.poller import FETCHERS


@pytest.mark.asyncio
async def test_disabled_by_default_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray ``provider='mock'`` source on a real deploy must FAIL its poll
    (failure-backoff path), never fabricate jobs into the catalog."""
    monkeypatch.setattr(settings, "mock_fetcher_enabled", False)
    with pytest.raises(RuntimeError, match="disabled"):
        await fetch_mock_jobs("loadtest:10")


@pytest.mark.asyncio
async def test_deterministic_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mock_fetcher_enabled", True)
    a = await fetch_mock_jobs("loadtest:25")
    b = await fetch_mock_jobs("loadtest:25")
    assert a == b
    assert len(a) == 25
    assert len({j.external_id for j in a}) == 25  # unique, stable ids
    # Feed shape survives the poller's free gates: US-parseable locations,
    # non-empty titles/descriptions, absolute URLs.
    assert all(j.location_name for j in a)
    assert all(j.title for j in a)
    assert all(len(j.content) > 500 for j in a)
    assert all(j.absolute_url.startswith("https://example.com/") for j in a)


@pytest.mark.asyncio
async def test_count_parsing_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mock_fetcher_enabled", True)
    assert len(await fetch_mock_jobs("t")) == 150  # default
    assert len(await fetch_mock_jobs("t:3")) == 3
    assert len(await fetch_mock_jobs("t:not-a-number")) == 150  # fallback
    assert len(await fetch_mock_jobs("t:0")) == 1  # floor
    assert len(await fetch_mock_jobs(f"t:{_MAX_JOB_COUNT + 5000}")) == _MAX_JOB_COUNT


@pytest.mark.asyncio
async def test_distinct_tokens_yield_distinct_external_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple mock sources in one poll must not collide on external_id
    (the jobs upsert conflicts on (source_id, external_id), but distinct ids
    keep the dedupe + funnel counters honest)."""
    monkeypatch.setattr(settings, "mock_fetcher_enabled", True)
    a = await fetch_mock_jobs("board-a:10")
    b = await fetch_mock_jobs("board-b:10")
    assert {j.external_id for j in a}.isdisjoint({j.external_id for j in b})


def test_registered_in_fetchers() -> None:
    assert FETCHERS["mock"] is fetch_mock_jobs
