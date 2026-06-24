"""Ingestion resilience: failure-cause persistence, auto-recovery, and the
health-check alerts that would have caught the 10-day silent outage.

Covers:
  - ``_record_source_failure`` persists last_error + last_error_at on every
    failure and stamps disabled_at + alerts when the backoff disables a source.
  - ``recover_stale_sources`` re-enables sources whose disabled_at is older
    than the cooldown.
  - ``check_ingestion_health`` fires the "no new jobs in N hours" and
    mass-disable Sentry alerts (and stays quiet when healthy).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings as live_settings
from app.services import ingestion_health as health_mod
from app.services import poller as poller_mod


class _Resp:
    def __init__(self, data: Any = None, count: int | None = None) -> None:
        self.data = data
        self.count = count


# ---------------------------------------------------------------------------
# _record_source_failure
# ---------------------------------------------------------------------------


def _capture_source_update() -> tuple[MagicMock, dict[str, Any]]:
    """A supabase mock that records the payload passed to
    ``sources.update(...)``."""
    captured: dict[str, Any] = {}

    def _update(payload: dict[str, Any]) -> MagicMock:
        captured.update(payload)
        handle = MagicMock()
        handle.eq.return_value.execute.return_value = _Resp(data=[])
        return handle

    sources_table = MagicMock()
    sources_table.update.side_effect = _update
    sb = MagicMock()
    sb.table.return_value = sources_table
    return sb, captured


@pytest.mark.asyncio
async def test_record_failure_persists_last_error_below_threshold(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "source_failure_disable_threshold", 10)
    sb, captured = _capture_source_update()
    source = {"id": "s1", "company_name": "Acme", "consecutive_failures": 2}

    await poller_mod._record_source_failure(sb, source, error="ConnectError('boom')")

    assert captured["consecutive_failures"] == 3
    assert captured["last_error"] == "ConnectError('boom')"
    assert captured["last_error_at"] is not None
    # Below threshold → not disabled, no disabled_at stamp.
    assert "enabled" not in captured
    assert "disabled_at" not in captured


@pytest.mark.asyncio
async def test_record_failure_disables_and_stamps_at_threshold(monkeypatch) -> None:
    """The regression guard for the outage: hitting the threshold must set
    enabled=false AND stamp disabled_at (drives auto-recovery)."""
    monkeypatch.setattr(live_settings, "source_failure_disable_threshold", 10)
    sb, captured = _capture_source_update()
    # 9 prior + this one == 10 == threshold.
    source = {"id": "s1", "company_name": "Acme", "consecutive_failures": 9}

    await poller_mod._record_source_failure(sb, source, error="HTTP 503")

    assert captured["consecutive_failures"] == 10
    assert captured["enabled"] is False
    assert captured["disabled_at"] is not None
    assert captured["last_error"] == "HTTP 503"


@pytest.mark.asyncio
async def test_record_failure_alerts_on_disable(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "source_failure_disable_threshold", 3)
    monkeypatch.setattr(live_settings, "sentry_dsn", "https://x@sentry/1")
    sb, _ = _capture_source_update()

    import sentry_sdk

    captured_msgs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda msg, level=None: captured_msgs.append((msg, level)),
    )

    source = {"id": "s1", "company_name": "Acme", "consecutive_failures": 2}
    await poller_mod._record_source_failure(sb, source, error="boom")

    assert len(captured_msgs) == 1
    assert "auto-disabled" in captured_msgs[0][0]
    assert captured_msgs[0][1] == "error"


@pytest.mark.asyncio
async def test_record_failure_truncates_huge_error(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "source_failure_disable_threshold", 10)
    sb, captured = _capture_source_update()
    source = {"id": "s1", "company_name": "Acme", "consecutive_failures": 0}

    await poller_mod._record_source_failure(sb, source, error="x" * 5000)

    assert len(captured["last_error"]) == poller_mod._SOURCE_LAST_ERROR_MAX_LEN


@pytest.mark.asyncio
async def test_record_failure_noop_when_backoff_disabled(monkeypatch) -> None:
    """threshold=0 disables the backoff entirely — no write at all."""
    monkeypatch.setattr(live_settings, "source_failure_disable_threshold", 0)
    sb = MagicMock()
    await poller_mod._record_source_failure(sb, {"id": "s1"}, error="boom")
    sb.table.assert_not_called()


# ---------------------------------------------------------------------------
# recover_stale_sources
# ---------------------------------------------------------------------------


def _recovery_supabase(recovered_rows: list[dict[str, Any]]) -> tuple[MagicMock, dict]:
    """A supabase mock for the recovery update chain
    ``update().eq().not_.is_().lt().execute()`` that records the filter
    args and returns ``recovered_rows`` as the affected set."""
    seen: dict[str, Any] = {}

    update_handle = MagicMock()

    def _update(payload: dict[str, Any]) -> MagicMock:
        seen["payload"] = payload
        return update_handle

    def _lt(col: str, val: str) -> MagicMock:
        seen["lt"] = (col, val)
        leaf = MagicMock()
        leaf.execute.return_value = _Resp(data=recovered_rows)
        return leaf

    # update().eq(...) -> .not_.is_(...) -> .lt(...) -> .execute()
    update_handle.eq.return_value.not_.is_.return_value.lt.side_effect = _lt

    sources_table = MagicMock()
    sources_table.update.side_effect = _update
    sb = MagicMock()
    sb.table.return_value = sources_table
    return sb, seen


@pytest.mark.asyncio
async def test_recovery_reenables_sources_past_cooldown(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "source_recovery_after_hours", 24)
    rows = [{"id": "s1", "company_name": "Acme"}, {"id": "s2", "company_name": "Beta"}]
    sb, seen = _recovery_supabase(rows)
    now = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)

    n = await poller_mod.recover_stale_sources(sb, now=now)

    assert n == 2
    # Re-enables and resets the counter + clears disabled_at.
    assert seen["payload"] == {
        "enabled": True,
        "consecutive_failures": 0,
        "disabled_at": None,
    }
    # Cutoff is exactly cooldown hours before `now`.
    expected_cutoff = (now - timedelta(hours=24)).isoformat()
    assert seen["lt"] == ("disabled_at", expected_cutoff)


@pytest.mark.asyncio
async def test_recovery_disabled_when_cooldown_zero(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "source_recovery_after_hours", 0)
    sb = MagicMock()
    n = await poller_mod.recover_stale_sources(sb)
    assert n == 0
    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "source_recovery_after_hours", 24)
    sb = MagicMock()
    sb.table.side_effect = RuntimeError("db down")
    # Must swallow and return 0, not raise.
    assert await poller_mod.recover_stale_sources(sb) == 0


# ---------------------------------------------------------------------------
# check_ingestion_health
# ---------------------------------------------------------------------------


def _health_supabase(
    *, newest_created_at: str | None, total: int, disabled: int
) -> MagicMock:
    """A supabase mock answering the health-check queries:
      - jobs: select().order().limit().execute() → newest created_at row
      - sources: select(count=exact) total, and .eq(enabled,False) disabled
    """
    jobs_leaf = MagicMock()
    jobs_rows = [{"created_at": newest_created_at}] if newest_created_at else []
    jobs_leaf.execute.return_value = _Resp(data=jobs_rows)
    jobs_table = MagicMock()
    jobs_table.select.return_value.order.return_value.limit.return_value = jobs_leaf

    sources_table = MagicMock()
    select_handle = MagicMock()
    # total count (no .eq)
    select_handle.execute.return_value = _Resp(data=[], count=total)
    # disabled count (.eq("enabled", False))
    select_handle.eq.return_value.execute.return_value = _Resp(data=[], count=disabled)
    sources_table.select.return_value = select_handle

    def _table(name: str) -> MagicMock:
        return jobs_table if name == "jobs" else sources_table

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


def _patch_sentry(monkeypatch) -> list[tuple[str, str]]:
    monkeypatch.setattr(live_settings, "sentry_dsn", "https://x@sentry/1")
    import sentry_sdk

    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda msg, level=None: captured.append((msg, level)),
    )
    return captured


@pytest.mark.asyncio
async def test_health_alerts_on_no_new_jobs(monkeypatch) -> None:
    """The highest-value alert: max(jobs.created_at) older than the
    threshold fires the 'no new jobs' alert."""
    monkeypatch.setattr(live_settings, "ingestion_health_check_enabled", True)
    monkeypatch.setattr(live_settings, "ingestion_max_job_age_hours", 48)
    monkeypatch.setattr(live_settings, "ingestion_mass_disable_ratio", 0.5)
    captured = _patch_sentry(monkeypatch)

    now = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    # Newest job is 5 days old — well past the 48h threshold.
    stale = (now - timedelta(days=5)).isoformat()
    sb = _health_supabase(newest_created_at=stale, total=10, disabled=0)

    report = await health_mod.check_ingestion_health(sb, now=now)

    assert report.stale_job_data is True
    assert report.mass_disable is False
    assert any("no new jobs" in m for m, _ in captured)


@pytest.mark.asyncio
async def test_health_alerts_on_mass_disable(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "ingestion_health_check_enabled", True)
    monkeypatch.setattr(live_settings, "ingestion_max_job_age_hours", 48)
    monkeypatch.setattr(live_settings, "ingestion_mass_disable_ratio", 0.5)
    captured = _patch_sentry(monkeypatch)

    now = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    fresh = (now - timedelta(hours=1)).isoformat()
    # 6 of 10 disabled == 60% >= 50% threshold.
    sb = _health_supabase(newest_created_at=fresh, total=10, disabled=6)

    report = await health_mod.check_ingestion_health(sb, now=now)

    assert report.mass_disable is True
    assert report.stale_job_data is False
    assert any("sources disabled" in m for m, _ in captured)


@pytest.mark.asyncio
async def test_health_quiet_when_healthy(monkeypatch) -> None:
    """Negative control: fresh jobs + few disabled sources → no alert."""
    monkeypatch.setattr(live_settings, "ingestion_health_check_enabled", True)
    monkeypatch.setattr(live_settings, "ingestion_max_job_age_hours", 48)
    monkeypatch.setattr(live_settings, "ingestion_mass_disable_ratio", 0.5)
    captured = _patch_sentry(monkeypatch)

    now = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    fresh = (now - timedelta(hours=2)).isoformat()
    sb = _health_supabase(newest_created_at=fresh, total=10, disabled=1)

    report = await health_mod.check_ingestion_health(sb, now=now)

    assert report.alerts == []
    assert captured == []


@pytest.mark.asyncio
async def test_health_alerts_when_no_jobs_at_all(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "ingestion_health_check_enabled", True)
    monkeypatch.setattr(live_settings, "ingestion_max_job_age_hours", 48)
    monkeypatch.setattr(live_settings, "ingestion_mass_disable_ratio", 0.5)
    captured = _patch_sentry(monkeypatch)

    now = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    sb = _health_supabase(newest_created_at=None, total=5, disabled=0)

    report = await health_mod.check_ingestion_health(sb, now=now)

    assert report.stale_job_data is True
    assert any("NO jobs" in m for m, _ in captured)


@pytest.mark.asyncio
async def test_health_disabled_by_flag(monkeypatch) -> None:
    monkeypatch.setattr(live_settings, "ingestion_health_check_enabled", False)
    captured = _patch_sentry(monkeypatch)
    sb = MagicMock()

    report = await health_mod.check_ingestion_health(sb)

    assert report.alerts == []
    assert captured == []
    sb.table.assert_not_called()
