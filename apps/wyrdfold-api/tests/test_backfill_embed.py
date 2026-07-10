"""#21 embedding sweep: drain the vector-less-job backlog every cycle.

``poller._backfill_embed_missing`` selects up to ``limit`` jobs with NO
``job_embeddings`` row (PostgREST anti-join, newest first, archived included)
and hands them to the existing ``_embed_jobs`` fan-out. Mirrors the #285
qualification sweep's shape: bounded, best-effort, never raises into the
cycle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services import poller as poller_mod


def _sweep_supabase(rows: list[dict[str, Any]], calls: dict[str, Any]) -> MagicMock:
    """Fake supabase recording the select chain; returns ``rows``."""
    select_chain = MagicMock()
    select_chain.eq.return_value = select_chain
    select_chain.is_.return_value = select_chain
    select_chain.order.return_value = select_chain

    def _limit(n: int) -> MagicMock:
        calls["limit"] = n
        return select_chain

    select_chain.limit.side_effect = _limit
    select_chain.execute = MagicMock(return_value=MagicMock(data=rows))

    def _select(cols: str) -> MagicMock:
        calls["select"] = cols
        return select_chain

    table = MagicMock()
    table.select.side_effect = _select

    sb = MagicMock()
    sb.table.return_value = table
    calls["chain"] = select_chain
    return sb


def _patch_embed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rec: dict[str, Any] = {"embedded": []}

    async def fake_embed_jobs(_sb: object, rows: list[dict[str, Any]]) -> None:
        rec["embedded"].extend(r["id"] for r in rows)

    monkeypatch.setattr(poller_mod, "_embed_jobs", fake_embed_jobs)
    return rec


class TestBackfillEmbedMissing:
    @pytest.mark.asyncio
    async def test_embeds_selected_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {"id": "j1", "title": "SWE", "description_html": "<p>x</p>"},
            {"id": "j2", "title": "PM", "description_html": "<p>y</p>"},
        ]
        calls: dict[str, Any] = {}
        sb = _sweep_supabase(rows, calls)
        rec = _patch_embed(monkeypatch)

        await poller_mod._backfill_embed_missing(sb, 150)

        assert rec["embedded"] == ["j1", "j2"]
        assert calls["limit"] == 150
        # The anti-join needs the embedded resource in the select AND the
        # is-null filter on it — otherwise this silently degrades to
        # re-selecting already-vectored jobs. It must also be MODEL-aware:
        # without the model filter, a stale voyage-3 row would mask the
        # missing voyage-3.5 vector and strand the old corpus forever.
        assert "job_embeddings" in calls["select"]
        calls["chain"].eq.assert_called_once_with(
            "job_embeddings.model", poller_mod.EMBED_DEFAULT_MODEL
        )
        # Two is-null filters: the anti-join itself, plus the purge exclusion
        # (tombstoned rows have no payload — embedding them would return
        # skipped_empty forever, wasting sweep slots).
        is_calls = [c.args for c in calls["chain"].is_.call_args_list]
        assert ("job_embeddings", "null") in is_calls
        assert ("purged_at", "null") in is_calls
        # Newest first: the drip is dominated by recent jobs about to face
        # the gate.
        calls["chain"].order.assert_called_once_with("created_at", desc=True)

    @pytest.mark.asyncio
    async def test_zero_or_negative_limit_is_a_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _patch_embed(monkeypatch)
        sb = MagicMock()

        await poller_mod._backfill_embed_missing(sb, 0)
        await poller_mod._backfill_embed_missing(sb, -5)

        sb.table.assert_not_called()
        assert rec["embedded"] == []

    @pytest.mark.asyncio
    async def test_empty_selection_skips_embed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: dict[str, Any] = {}
        sb = _sweep_supabase([], calls)
        rec = _patch_embed(monkeypatch)

        await poller_mod._backfill_embed_missing(sb, 50)

        assert rec["embedded"] == []

    @pytest.mark.asyncio
    async def test_select_failure_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A DB error must never raise into the poll cycle — the sweep just
        # skips this cycle and retries next time.
        rec = _patch_embed(monkeypatch)
        table = MagicMock()
        table.select.side_effect = RuntimeError("db down")
        sb = MagicMock()
        sb.table.return_value = table

        await poller_mod._backfill_embed_missing(sb, 50)  # must not raise

        assert rec["embedded"] == []
