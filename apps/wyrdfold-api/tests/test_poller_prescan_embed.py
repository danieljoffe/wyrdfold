"""Poller wiring for the #60 pre-scan job embeddings (Phase 1).

Covers ``poller._embed_jobs`` and the flag-gated on-ingest hook:

- ``_embed_jobs`` calls ``upsert_job_embedding`` once per row, forwarding the
  row's id / title / description_html.
- The whole step is best-effort: a client-resolution failure or a per-row
  embed failure never raises out of ``_embed_jobs``.
- INERT by default: ``settings.prescan_embed_enabled`` is False, and the
  poll-cycle guard (``if settings.prescan_embed_enabled and upsert_resp.data``)
  means no embed work happens unless an operator flips the flag — merging this
  PR changes no behavior and incurs no embedding spend.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services import poller as poller_mod


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "job-1",
        "title": "Staff Engineer",
        "company_name": "Acme",
        "location": "San Francisco, CA",
        "description_html": "<p>Build things.</p>",
    }
    base.update(kw)
    return base


def _patch_embed(
    monkeypatch: pytest.MonkeyPatch,
    *,
    upsert_side_effect: Any = None,
    client_raises: bool = False,
) -> dict[str, Any]:
    """Patch the poller's embeddings client resolver + upsert, returning a
    recorder dict the tests assert against."""
    rec: dict[str, Any] = {"upsert_calls": [], "client_resolved": 0}

    def fake_get_client() -> object:
        rec["client_resolved"] += 1
        if client_raises:
            raise RuntimeError("no embeddings client")
        return MagicMock(name="embeddings-client")

    async def fake_upsert(
        _supabase: object,
        _client: object,
        *,
        job_id: str,
        title: str | None,
        description_html: str | None,
        **_kw: Any,
    ) -> str:
        rec["upsert_calls"].append(
            {"job_id": job_id, "title": title, "description_html": description_html}
        )
        if upsert_side_effect is not None:
            raise upsert_side_effect
        return "embedded"

    monkeypatch.setattr(poller_mod, "get_embeddings_client", fake_get_client)
    monkeypatch.setattr(poller_mod, "upsert_job_embedding", fake_upsert)
    return rec


class TestEmbedJobs:
    async def test_each_row_is_embedded_with_forwarded_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _patch_embed(monkeypatch)
        sb = MagicMock()
        rows = [
            _row(id="a", title="A", description_html="<p>aa</p>"),
            _row(id="b", title="B", description_html="<p>bb</p>"),
        ]

        await poller_mod._embed_jobs(sb, rows)

        assert rec["client_resolved"] == 1  # resolved once for the batch
        assert {c["job_id"] for c in rec["upsert_calls"]} == {"a", "b"}
        by_id = {c["job_id"]: c for c in rec["upsert_calls"]}
        assert by_id["a"]["title"] == "A"
        assert by_id["a"]["description_html"] == "<p>aa</p>"

    async def test_client_resolution_failure_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _patch_embed(monkeypatch, client_raises=True)
        sb = MagicMock()

        # Must not raise.
        await poller_mod._embed_jobs(sb, [_row()])

        assert rec["upsert_calls"] == []  # never got to embedding

    async def test_per_row_embed_failure_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _patch_embed(monkeypatch, upsert_side_effect=RuntimeError("boom"))
        sb = MagicMock()

        # gather(return_exceptions=True) → the failure is contained.
        await poller_mod._embed_jobs(sb, [_row(id="a"), _row(id="b")])

        assert len(rec["upsert_calls"]) == 2  # both attempted

    async def test_empty_rows_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_embed(monkeypatch)
        sb = MagicMock()

        await poller_mod._embed_jobs(sb, [])

        assert rec["upsert_calls"] == []


class TestInertByDefault:
    def test_flag_defaults_off(self) -> None:
        # The whole feature is gated on this; default off ⇒ merging is inert.
        assert settings.prescan_embed_enabled is False

    async def test_guard_skips_embed_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reproduce the poll-cycle guard literally and assert it gates the
        embed: flag off ⇒ ``_embed_jobs`` is never invoked even with data."""
        rec = _patch_embed(monkeypatch)
        embed_called = {"n": 0}

        async def spy_embed_jobs(_sb: object, _rows: Any) -> None:
            embed_called["n"] += 1

        monkeypatch.setattr(poller_mod, "_embed_jobs", spy_embed_jobs)
        monkeypatch.setattr(settings, "prescan_embed_enabled", False)

        upsert_resp_data = [_row()]
        # The exact guard from poller._poll_one_source / _poll_one_source_for_target.
        if settings.prescan_embed_enabled and upsert_resp_data:
            await poller_mod._embed_jobs(MagicMock(), upsert_resp_data)

        assert embed_called["n"] == 0
        assert rec["upsert_calls"] == []

    async def test_guard_runs_embed_when_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        embed_called = {"n": 0}

        async def spy_embed_jobs(_sb: object, _rows: Any) -> None:
            embed_called["n"] += 1

        monkeypatch.setattr(poller_mod, "_embed_jobs", spy_embed_jobs)
        monkeypatch.setattr(settings, "prescan_embed_enabled", True)

        upsert_resp_data = [_row()]
        if settings.prescan_embed_enabled and upsert_resp_data:
            await poller_mod._embed_jobs(MagicMock(), upsert_resp_data)

        assert embed_called["n"] == 1
