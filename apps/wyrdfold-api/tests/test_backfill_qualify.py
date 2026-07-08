"""#285 backfill sweep: liveness-gated tagging + archival of dead listings.

Covers:
- ``validate.liveness_verdict`` — the live/dead/unknown classifier over a
  ``ValidationResult``. A 404 is ``is_valid=True`` (that flag only guards
  format/SSRF/banned), so deadness is read from ``final_status`` /
  ``looks_like_job``.
- ``poller._backfill_qualify_stale`` — selects the oldest untagged, unarchived
  jobs, TAGS the live ones through the budget-gated tagger, ARCHIVES the dead
  ones, and leaves UNKNOWN (transient) / URL-less rows untouched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services import poller as poller_mod
from app.services.validate import ValidationResult, liveness_verdict


def _vr(**kw: Any) -> ValidationResult:
    base: dict[str, Any] = {"is_valid": True, "final_url": "https://x/j"}
    base.update(kw)
    return ValidationResult(**base)


class TestLivenessVerdict:
    def test_live_200_job(self) -> None:
        assert liveness_verdict(_vr(final_status=200, looks_like_job=True)) == "live"

    def test_dead_404(self) -> None:
        assert liveness_verdict(_vr(final_status=404)) == "dead"

    def test_dead_410_gone(self) -> None:
        assert liveness_verdict(_vr(final_status=410)) == "dead"

    def test_unknown_200_not_a_job_not_archived(self) -> None:
        # A 200 whose body isn't a job (a "position filled" / listing page) is
        # NOT tagged, but NOT hard-archived either — _verify_content is a soft
        # signal, so a live job with odd markup can't be wrongly removed.
        assert liveness_verdict(_vr(final_status=200, looks_like_job=False)) == "unknown"

    def test_unknown_5xx_is_transient(self) -> None:
        assert liveness_verdict(_vr(final_status=503)) == "unknown"

    def test_unknown_fetch_failed_none_status(self) -> None:
        assert liveness_verdict(_vr(final_status=None)) == "unknown"

    def test_unknown_security_reject_never_dead(self) -> None:
        # is_valid=False (malformed/banned/SSRF) is never a clean dead signal —
        # don't archive on it.
        r = ValidationResult(is_valid=False, final_url="x", rejection_reason="banned_domain:x")
        assert liveness_verdict(r) == "unknown"

    def test_200_without_content_verdict_defaults_live(self) -> None:
        assert liveness_verdict(_vr(final_status=200, looks_like_job=None)) == "live"


def _backfill_supabase(rows: list[dict[str, Any]], archived: list[str]) -> MagicMock:
    """Fake supabase: the select chain returns ``rows``; a
    ``jobs.update(...).in_('id', ids)`` records the archived ids into
    ``archived``."""
    select_chain = MagicMock()
    select_chain.is_.return_value = select_chain
    select_chain.order.return_value = select_chain
    select_chain.limit.return_value = select_chain
    select_chain.execute = MagicMock(return_value=MagicMock(data=rows))

    def _in(_col: str, ids: list[str]) -> MagicMock:
        archived.extend(ids)
        chain = MagicMock()
        chain.execute = MagicMock(return_value=MagicMock(data=[]))
        return chain

    update_chain = MagicMock()
    update_chain.in_.side_effect = _in

    def _table(_name: str) -> MagicMock:
        t = MagicMock()
        t.select.return_value = select_chain
        t.update.return_value = update_chain
        return t

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


def _patch_backfill(monkeypatch: pytest.MonkeyPatch, verdicts: dict[str, str]) -> dict[str, Any]:
    """Patch ``validate_job_url`` (verdict keyed by url) + ``_qualify_jobs``
    (capture the rows it was asked to tag)."""
    rec: dict[str, Any] = {"tagged": []}

    async def fake_validate(url: str) -> ValidationResult:
        v = verdicts.get(url, "unknown")
        if v == "live":
            return ValidationResult(
                is_valid=True, final_url=url, final_status=200, looks_like_job=True
            )
        if v == "dead":
            return ValidationResult(is_valid=True, final_url=url, final_status=404)
        return ValidationResult(is_valid=True, final_url=url, final_status=503)

    async def fake_qualify(_supabase: object, rows: list[dict[str, Any]]) -> None:
        rec["tagged"].extend(r["id"] for r in rows)

    monkeypatch.setattr(poller_mod, "validate_job_url", fake_validate)
    monkeypatch.setattr(poller_mod, "_qualify_jobs", fake_qualify)
    return rec


class TestBackfillQualifyStale:
    @pytest.mark.asyncio
    async def test_tags_live_archives_dead_skips_unknown_and_urlless(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows: list[dict[str, Any]] = [
            {"id": "live-1", "absolute_url": "https://x/live1"},
            {"id": "dead-1", "absolute_url": "https://x/dead1"},
            {"id": "unk-1", "absolute_url": "https://x/unk1"},
            {"id": "nourl-1", "absolute_url": None},
        ]
        rec = _patch_backfill(
            monkeypatch,
            {
                "https://x/live1": "live",
                "https://x/dead1": "dead",
                "https://x/unk1": "unknown",
            },
        )
        archived: list[str] = []
        sb = _backfill_supabase(rows, archived)

        await poller_mod._backfill_qualify_stale(sb, limit=10)

        assert rec["tagged"] == ["live-1"]  # only the live one is tagged (spend)
        assert archived == ["dead-1"]  # only the dead one is archived
        # unk-1 (transient 5xx) and nourl-1 (no URL) touch neither path.

    @pytest.mark.asyncio
    async def test_limit_zero_is_noop_no_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_backfill(monkeypatch, {})
        archived: list[str] = []
        rows: list[dict[str, Any]] = [{"id": "x", "absolute_url": "https://x/j"}]
        sb = _backfill_supabase(rows, archived)

        await poller_mod._backfill_qualify_stale(sb, limit=0)

        assert rec["tagged"] == []
        assert archived == []
        sb.table.assert_not_called()  # returns before it even queries

    @pytest.mark.asyncio
    async def test_no_untagged_rows_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_backfill(monkeypatch, {})
        archived: list[str] = []
        sb = _backfill_supabase([], archived)

        await poller_mod._backfill_qualify_stale(sb, limit=10)

        assert rec["tagged"] == []
        assert archived == []
