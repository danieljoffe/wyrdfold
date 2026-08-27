"""Ingest is $0 LLM: the poll cycle must not buy qualification tags.

Tagging is LAZY now — ``qualification.materialize.ensure_job_tags`` runs at
grade time, for the trimmed candidate set that is about to read the tags (see
``test_phase2_runner.py``). Ingest classified EVERY newly-upserted or
content-changed row whether or not anyone would ever read it; these tests are
the regression that keeps that call from coming back.

Both ingest paths are covered — ``_poll_one_source`` (the scheduled/force
cycle) and ``_poll_one_source_for_target`` (target activation) — because the
old tagger was wired into both.

What ingest still writes is FREE and must NOT regress: ``board_columns`` (the
board's own remote / employment-type answer, #846/#851) and the deterministic
location/salary parses.
"""

from __future__ import annotations

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
    _job,
    _make_poll_supabase,
    _make_targeted_poll_supabase,
)

pytestmark = pytest.mark.asyncio


def _spy_on_the_tagger(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every route into the qualification tagger.

    Two seams, deliberately:

    * ``materialize.tag_job`` — the model call. A module global, resolved at
      call time, so it fires however a future change reaches the tagger
      (``ensure_job_tags``, ``_qualify_one_job``, a direct call).
    * ``poller.get_llm_client_async`` — the poller's OWN client factory. This
      is the one that makes the test non-vacuous: the tagger used to live
      inside ``poller.py`` and bind its own ``tag_job``/``get_llm_client_async``
      names, which a spy on ``materialize`` would sail straight past. On these
      ingest paths nothing else resolves an LLM client (Phase 1 is off and
      there are no Stage-3 users), so a non-zero count here IS ingest buying
      LLM work. Verified by running these tests against the pre-change poller:
      ``client_calls`` was 1 per path.
    """
    rec = {"tag_calls": 0, "client_calls": 0}

    async def spy_tag_job(*_a: Any, **_kw: Any) -> Any:
        rec["tag_calls"] += 1
        return None, None

    async def spy_client(*_a: Any, **_kw: Any) -> Any:
        rec["client_calls"] += 1
        return MagicMock()

    monkeypatch.setattr(materialize, "tag_job", spy_tag_job)
    monkeypatch.setattr(materialize, "get_llm_client_async", spy_client)
    monkeypatch.setattr(poller_mod, "get_llm_client_async", spy_client)
    return rec


def _upserted_row() -> dict[str, Any]:
    """A jobs-upsert RESULT row: full columns, untagged, live.

    This is what the old ingest tagger consumed — with the flag ON and a
    non-empty upsert result, the removed call site would fire here.
    """
    return {
        "id": "j1",
        "external_id": "k3",
        "title": "Staff Frontend Engineer",
        "company_name": "Acme",
        "location": "Remote",
        "description_html": "<p>Build things.</p>",
        "archived_at": None,
        "qualified_hash": None,
        "qualified_at": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


async def _fetch_one(_token: str) -> list[StandardJob]:
    return [_job("k3", "Staff Frontend Engineer", "Remote")]


def _open_gate() -> MagicMock:
    """A budget snapshot with a live consumer.

    Load-bearing for the assertion, not decoration: the ingest tagger skipped
    the model entirely when the cycle's gate reported EVERY target blocked, and
    an unconfigured mock gate reports exactly that (a MagicMock return is
    truthy). Without an open gate the "zero LLM calls" assertion would hold for
    the wrong reason.
    """
    gate = MagicMock()
    gate.target_blocked.return_value = False
    gate.user_blocked.return_value = False
    return gate


async def test_shared_cycle_ingest_buys_no_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_poll_one_source``: a brand-new row lands with ZERO tagger calls."""
    monkeypatch.setattr(live_settings, "qualification_enabled", True)  # flag ON
    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, _sources = _make_poll_supabase([])
    jobs_table.upsert.return_value.execute.return_value.data = [_upserted_row()]
    rec = _spy_on_the_tagger(monkeypatch)

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", _fetch_one)
    monkeypatch.setattr(
        poller_mod,
        "_active_targets",
        AsyncMock(return_value=[_full_target(app_active=True, search_keywords=["frontend"])]),
    )
    monkeypatch.setattr(
        poller_mod, "_resolve_user_targets_for_stage3", AsyncMock(return_value=({}, {}))
    )

    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE), supabase, budget_gate=_open_gate()
    )

    # Preconditions: the row really was ingested, so the zeros below mean
    # "ingest no longer tags", not "nothing happened".
    assert summary["error"] is None
    assert jobs_table.upsert.called
    assert summary["new"] == 1
    assert rec == {"tag_calls": 0, "client_calls": 0}


async def test_target_activation_ingest_buys_no_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_poll_one_source_for_target``: same contract on the activation path."""
    monkeypatch.setattr(live_settings, "qualification_enabled", True)  # flag ON
    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, _user_targets = _make_targeted_poll_supabase()
    jobs_table.upsert.return_value.execute.return_value.data = [_upserted_row()]
    # The junction is empty, so there are no Stage-3 users and nothing on this
    # path resolves an LLM client except the (removed) ingest tagger.
    rec = _spy_on_the_tagger(monkeypatch)

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", _fetch_one)

    summary = await poller_mod._poll_one_source_for_target(
        dict(_GUARD_SOURCE),
        supabase,
        _full_target(app_active=True, search_keywords=["frontend engineer"]),
        payer_user_id="payer-1",
    )

    assert summary["error"] is None
    assert jobs_table.upsert.called
    assert summary["new"] == 1
    assert rec == {"tag_calls": 0, "client_calls": 0}


async def test_ingest_still_writes_the_free_board_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#846/#851 must survive the tagger's departure: the board's own answer is
    deterministic and free, so it still lands at upsert. Tag columns must NOT
    (nothing bought them)."""
    monkeypatch.setattr(live_settings, "qualification_enabled", True)
    monkeypatch.setattr(live_settings, "phase1_triage_enabled", False)
    monkeypatch.setattr(live_settings, "validate_poll_urls", False)

    supabase, jobs_table, _sources = _make_poll_supabase([])
    jobs_table.upsert.return_value.execute.return_value.data = [_upserted_row()]
    _spy_on_the_tagger(monkeypatch)

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", _fetch_one)
    monkeypatch.setattr(
        poller_mod,
        "_active_targets",
        AsyncMock(return_value=[_full_target(app_active=True, search_keywords=["frontend"])]),
    )
    monkeypatch.setattr(
        poller_mod, "_resolve_user_targets_for_stage3", AsyncMock(return_value=({}, {}))
    )

    await poller_mod._poll_one_source(dict(_GUARD_SOURCE), supabase, budget_gate=_open_gate())

    written = jobs_table.upsert.call_args[0][0][0]
    # The board said "Remote" in its location string — deterministic, free.
    assert written["is_remote"] is True
    # ...and nothing bought a tag, so no tag column is written at ingest.
    for tag_col in ("role_family", "seniority", "metro", "is_genuine_role", "qualified_at"):
        assert tag_col not in written, tag_col
