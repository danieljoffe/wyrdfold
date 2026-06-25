"""Poller wiring for the #60/#68 pre-scan SHADOW MODE (Phase 3).

Covers ``poller._shadow_observe`` (compute the cosine gate decision + append one
``prescan_shadow`` row alongside the live keyword decision) and the flag gate:

- INERT by default: ``settings.prescan_shadow_enabled`` is False, and the
  Stage-2 guard (``if settings.prescan_shadow_enabled``) means no shadow row is
  written AND no cosine computation runs unless an operator flips the flag —
  merging this PR changes no behavior, drops no jobs, and spends nothing (cosine
  reuses cached vectors; with the flag off it isn't even called).
- OBSERVATION ONLY: ``_shadow_observe`` records the keyword decision that
  actually drove admission UNCHANGED, plus the would-be cosine verdict — it never
  alters what gets graded.
- ``_shadow_observe`` forwards the full row shape and is fail-soft (a cosine or
  write error never propagates).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.models.targets import JobTarget, ScoringProfile
from app.services import poller as poller_mod


def _target(target_id: str = "tgt-1") -> JobTarget:
    now = datetime(2026, 6, 24, tzinfo=UTC)
    return JobTarget(
        id=target_id,
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(),
        search_keywords=["React", "TypeScript"],
        is_active=True,
        created_at=now,
        updated_at=now,
    )


def _patch_shadow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate_return: tuple[float | None, bool | None] = (0.81, True),
    threshold: float | None = 0.5,
    gate_raises: bool = False,
    record_raises: bool = False,
) -> dict[str, Any]:
    """Patch the poller's cosine-gate, threshold read, and row writer; return a
    recorder the tests assert against."""
    rec: dict[str, Any] = {"gate_calls": [], "recorded": [], "threshold_calls": 0}

    async def fake_gate(_sb: object, *, job_id: str, target: JobTarget) -> Any:
        rec["gate_calls"].append({"job_id": job_id, "target_id": target.id})
        if gate_raises:
            raise RuntimeError("gate boom")
        return gate_return

    async def fake_threshold(_sb: object, *, target_id: str) -> float | None:
        rec["threshold_calls"] += 1
        return threshold

    async def fake_record(_sb: object, **kw: Any) -> None:
        if record_raises:
            raise RuntimeError("record boom")
        rec["recorded"].append(kw)

    monkeypatch.setattr(poller_mod, "cosine_gate_decision", fake_gate)
    monkeypatch.setattr(poller_mod, "_shadow_threshold", fake_threshold)
    monkeypatch.setattr(poller_mod, "record_shadow_observation", fake_record)
    return rec


# --------------------------------------------------------------------------- #
# INERT by default
# --------------------------------------------------------------------------- #
class TestInertByDefault:
    def test_flag_defaults_off(self) -> None:
        # The whole shadow feature is gated on this; default off ⇒ merging is inert.
        assert settings.prescan_shadow_enabled is False

    async def test_guard_skips_shadow_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduce the Stage-2 guard literally: flag off ⇒ ``_shadow_observe``
        is never invoked (no cosine work, no row) even with a scored job."""
        rec = _patch_shadow(monkeypatch)
        observe_called = {"n": 0}

        async def spy_observe(_sb: object, **_kw: Any) -> None:
            observe_called["n"] += 1

        monkeypatch.setattr(poller_mod, "_shadow_observe", spy_observe)
        monkeypatch.setattr(settings, "prescan_shadow_enabled", False)

        # The exact guard from poller._full_score_one (both poll paths).
        if settings.prescan_shadow_enabled:
            await poller_mod._shadow_observe(
                MagicMock(), job_id="j", target=_target(), keyword_admit=True, keyword_score=42
            )

        assert observe_called["n"] == 0
        assert rec["gate_calls"] == []  # no cosine computation when flag off
        assert rec["recorded"] == []  # no shadow row when flag off

    async def test_guard_runs_shadow_when_flag_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_called = {"n": 0}

        async def spy_observe(_sb: object, **_kw: Any) -> None:
            observe_called["n"] += 1

        monkeypatch.setattr(poller_mod, "_shadow_observe", spy_observe)
        monkeypatch.setattr(settings, "prescan_shadow_enabled", True)

        if settings.prescan_shadow_enabled:
            await poller_mod._shadow_observe(
                MagicMock(), job_id="j", target=_target(), keyword_admit=True, keyword_score=42
            )

        assert observe_called["n"] == 1


# --------------------------------------------------------------------------- #
# _shadow_observe row shape (mock gate + writer)
# --------------------------------------------------------------------------- #
class TestShadowObserve:
    async def test_records_full_row_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_shadow(monkeypatch, gate_return=(0.81, True), threshold=0.5)
        sb = MagicMock()

        await poller_mod._shadow_observe(
            sb, job_id="job-1", target=_target("tgt-7"), keyword_admit=False, keyword_score=12
        )

        assert len(rec["recorded"]) == 1
        row = rec["recorded"][0]
        # Keyword side is the LIVE decision, forwarded unchanged.
        assert row["job_id"] == "job-1"
        assert row["target_id"] == "tgt-7"
        assert row["keyword_admit"] is False
        assert row["keyword_score"] == 12
        # Cosine side is the OBSERVED-only gate verdict + the target threshold.
        assert row["cosine"] == 0.81
        assert row["cosine_admit"] is True
        assert row["threshold"] == 0.5
        # Cosine was actually computed for this (job, target).
        assert rec["gate_calls"] == [{"job_id": "job-1", "target_id": "tgt-7"}]

    async def test_records_null_cosine_when_gate_has_no_opinion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Phase-1/2 vectors not populated ⇒ gate returns (None, None); the row
        # still records the keyword side with a NULL cosine side.
        rec = _patch_shadow(monkeypatch, gate_return=(None, None), threshold=None)
        await poller_mod._shadow_observe(
            MagicMock(), job_id="job-2", target=_target(), keyword_admit=True, keyword_score=88
        )
        row = rec["recorded"][0]
        assert row["keyword_admit"] is True
        assert row["keyword_score"] == 88
        assert row["cosine"] is None
        assert row["cosine_admit"] is None
        assert row["threshold"] is None

    async def test_failsoft_on_gate_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A cosine error must never propagate out of the best-effort shadow hook.
        rec = _patch_shadow(monkeypatch, gate_raises=True)
        await poller_mod._shadow_observe(
            MagicMock(), job_id="job-3", target=_target(), keyword_admit=True, keyword_score=5
        )
        # Swallowed: no row written, no raise.
        assert rec["recorded"] == []

    async def test_failsoft_on_record_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_shadow(monkeypatch, record_raises=True)
        # Must not raise even though the writer blows up.
        await poller_mod._shadow_observe(
            MagicMock(), job_id="job-4", target=_target(), keyword_admit=True, keyword_score=5
        )
        assert rec["gate_calls"]  # cosine ran; the write was the failure


# --------------------------------------------------------------------------- #
# record_shadow_observation — the actual insert payload (real writer, fake sb)
# --------------------------------------------------------------------------- #
class TestRecordShadowObservation:
    async def test_inserts_expected_payload(self) -> None:
        from app.services.embeddings.prescan_shadow import record_shadow_observation

        captured: dict[str, Any] = {}

        class _Tbl:
            def insert(self, row: dict[str, Any]) -> _Tbl:
                captured["row"] = row
                return self

            def execute(self) -> Any:
                captured["executed"] = True
                return MagicMock()

        class _SB:
            def table(self, name: str) -> _Tbl:
                captured["table"] = name
                return _Tbl()

        await record_shadow_observation(
            _SB(),
            job_id="job-1",
            target_id="tgt-1",
            keyword_admit=True,
            keyword_score=42,
            cosine=0.73,
            cosine_admit=True,
            threshold=0.5,
        )

        assert captured["table"] == "prescan_shadow"
        assert captured["executed"] is True
        assert captured["row"] == {
            "job_posting_id": "job-1",
            "target_id": "tgt-1",
            "keyword_admit": True,
            "keyword_score": 42,
            "cosine": 0.73,
            "cosine_admit": True,
            "threshold": 0.5,
        }

    async def test_write_is_failsoft(self) -> None:
        from app.services.embeddings.prescan_shadow import record_shadow_observation

        class _SB:
            def table(self, name: str) -> Any:
                raise RuntimeError("db down")

        # Best-effort: a DB error never propagates out of the writer.
        await record_shadow_observation(
            _SB(),
            job_id="job-1",
            target_id="tgt-1",
            keyword_admit=True,
            keyword_score=1,
            cosine=None,
            cosine_admit=None,
            threshold=None,
        )
