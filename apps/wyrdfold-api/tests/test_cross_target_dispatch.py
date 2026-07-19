"""Dispatch logic for the untargeted list (#365): RPC vs two-query.

``_list_jobs_across_user_targets`` tries the ``get_cross_target_jobs`` RPC for
the shapes it can express DB-side and falls back to the Python two-query path
for the rest. These pin that routing decision with spies (the RPC/two-query
bodies have their own tests), so a future edit can't silently send an
RPC-ineligible query (custom weights, recency decay, post-fetch filters, the
archived view) down the fast path — or fail to use the fast path at all."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.models.targets import AxisWeights
from app.routers import jobs as jobs_mod
from app.routers.jobs import (
    _list_jobs_across_user_targets,
    _LogisticsFilter,
    _RpcIneligibleError,
)


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    rpc = MagicMock(return_value={"postings": [], "next_cursor": None, "total": None})
    two = MagicMock(return_value={"postings": [], "next_cursor": None, "total": 0})
    monkeypatch.setattr(jobs_mod, "_list_jobs_across_user_targets_rpc", rpc)
    monkeypatch.setattr(jobs_mod, "_list_jobs_across_user_targets_two_query", two)
    # Decay off by default (prod posture); individual tests flip it.
    monkeypatch.setattr(settings, "recency_decay_enabled", False)
    return {"rpc": rpc, "two": two}


def _call(**overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "user_target_ids": {"t-1", "t-2"},
        "page_size": 5,
        "sort": "score",
        "ascending": False,
        "min_score": None,
        "status": "new",
        "company": None,
        "search": None,
        "exclude_terms": [],
        "only_terms": [],
        "cursor": {},
        "weights_by_target": None,
        "user_id": "u-1",
        "logistics": None,
    }
    kwargs.update(overrides)
    _list_jobs_across_user_targets(MagicMock(), **kwargs)


def test_simple_status_query_uses_rpc(spies: dict[str, MagicMock]) -> None:
    _call(status="new", sort="score")
    spies["rpc"].assert_called_once()
    spies["two"].assert_not_called()


def test_custom_weights_force_two_query(spies: dict[str, MagicMock]) -> None:
    _call(weights_by_target={"t-1": AxisWeights()})
    spies["two"].assert_called_once()
    spies["rpc"].assert_not_called()


def test_recency_decay_still_uses_rpc(
    spies: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Decay is NOT a skip: the RPC ranks by the decayed score DB-side
    # (p_recency_decay), so the fast path still engages when decay is on (prod).
    monkeypatch.setattr(settings, "recency_decay_enabled", True)
    _call()
    spies["rpc"].assert_called_once()
    spies["two"].assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"exclude_terms": ["remote"]},
        {"only_terms": ["berlin"]},
        {"logistics": _LogisticsFilter(remote_only=True)},
        {"status": "archived"},
    ],
    ids=["location-exclude", "location-only", "logistics", "archived-view"],
)
def test_post_fetch_and_archived_force_two_query(
    spies: dict[str, MagicMock], overrides: dict[str, Any]
) -> None:
    _call(**overrides)
    spies["two"].assert_called_once()
    spies["rpc"].assert_not_called()


def test_rpc_error_falls_back_to_two_query(
    spies: dict[str, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    # A GENUINE RPC failure (not a shape it can't express) must degrade LOUDLY —
    # a WARNING carrying the traceback — so silent reversion to the slower
    # two-query path is visible on prod (audit 2026-07-18 PERF-M / silent-degrade;
    # it logged at DEBUG before, invisible under the INFO root).
    spies["rpc"].side_effect = RuntimeError("RPC unavailable")
    with caplog.at_level(logging.WARNING, logger="app.routers.jobs"):
        _call()
    spies["rpc"].assert_called_once()
    spies["two"].assert_called_once()  # graceful fallback
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("get_cross_target_jobs RPC FAILED" in r.getMessage() for r in warned), caplog.text
    assert any(r.exc_info for r in warned), "the WARNING must carry the traceback"


def test_rpc_ineligible_falls_back_quietly(
    spies: dict[str, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    # An RPC-INELIGIBLE shape (a helper raising _RpcIneligibleError) is an
    # EXPECTED fallback, not a failure — it stays quiet (DEBUG) so the WARNING
    # channel is reserved for genuine RPC breakage. Same graceful fallback.
    spies["rpc"].side_effect = _RpcIneligibleError("RPC path skipped: shape X")
    with caplog.at_level(logging.DEBUG, logger="app.routers.jobs"):
        _call()
    spies["two"].assert_called_once()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "an ineligible shape must not warn: " + caplog.text
    )
