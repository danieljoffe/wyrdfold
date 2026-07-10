"""Archival lifecycle sweep (UX/IA §5, Stage 1 + Stage 2 A/B).

The guards ARE the feature — most tests here prove the sweep REFUSES to
touch rows it must not:

- Stage 1 never archives an engaged job (any user_jobs status beyond
  new/archived).
- Stage 2 never purges an engaged job in either form; tombstones (B) graded
  or still-listed rows instead of deleting; hard-deletes (A) only pure noise
  (untouched + ungraded + delisted past the grace window).
- The poller's purge guard refuses to resurrect tombstoned rows on re-poll,
  and fails soft (on error, upsert everything).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import archival
from app.services import poller as poller_mod


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


class _FakeQuery:
    """Chainable query stub: every builder method records and returns self;
    ``execute`` returns the seeded rows. ``not_`` chains as an attribute."""

    def __init__(self, table: str, result: list[dict[str, Any]], log: list[Any]):
        self.table = table
        self._result = result
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        log.append(self)

    def __getattr__(self, name: str) -> Any:
        if name == "execute":
            return lambda: SimpleNamespace(data=list(self._result))
        if name == "not_":
            self.calls.append(("not_", ()))
            return self

        def _method(*args: Any, **kwargs: Any) -> _FakeQuery:
            self.calls.append((name, args))
            return self

        return _method

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args in self.calls if n == name]


class _FakeSupabase:
    """Seeds per-table FIFO results; records every query for assertions."""

    def __init__(self, results: dict[str, list[list[dict[str, Any]]]]):
        self._results = {k: list(v) for k, v in results.items()}
        self.log: list[_FakeQuery] = []

    def table(self, name: str) -> _FakeQuery:
        queue = self._results.get(name, [])
        result = queue.pop(0) if queue else []
        return _FakeQuery(name, result, self.log)

    # -- assertion helpers -------------------------------------------------
    def ops(self, table: str, op: str) -> list[_FakeQuery]:
        return [q for q in self.log if q.table == table and q.called(op)]


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(archival.settings, "archival_sweep_enabled", True)
    monkeypatch.setattr(archival.settings, "archival_archive_after_days", 30)
    monkeypatch.setattr(archival.settings, "archival_purge_after_days", 60)
    monkeypatch.setattr(archival.settings, "archival_delist_grace_days", 14)
    monkeypatch.setattr(archival.settings, "archival_sweep_batch", 500)


class TestArchiveStale:
    @pytest.mark.asyncio
    async def test_archives_untouched_but_never_engaged_jobs(self) -> None:
        sb = _FakeSupabase(
            {
                # Stage-1 candidate scan → three old live jobs.
                "jobs": [[{"id": "old-1"}, {"id": "old-2"}, {"id": "old-3"}]],
                # old-2 was applied to by someone → engaged.
                "user_jobs": [[{"job_posting_id": "old-2"}]],
            }
        )
        archived = await archival._archive_stale(sb, batch=500)

        assert archived == 2
        updates = sb.ops("jobs", "update")
        assert len(updates) == 1
        (payload,) = updates[0].called("update")[0]
        assert "archived_at" in payload
        (in_args,) = updates[0].called("in_")
        assert sorted(in_args[1]) == ["old-1", "old-3"]  # old-2 protected

    @pytest.mark.asyncio
    async def test_no_candidates_is_a_noop(self) -> None:
        sb = _FakeSupabase({"jobs": [[]]})
        assert await archival._archive_stale(sb, batch=500) == 0
        assert sb.ops("jobs", "update") == []


class TestPurgeOld:
    @pytest.mark.asyncio
    async def test_ab_split_deletes_noise_tombstones_history(self) -> None:
        stale = _days_ago(30)  # well past the 14d delist grace
        fresh = _days_ago(1)  # still being re-upserted by its source
        sb = _FakeSupabase(
            {
                "jobs": [
                    [
                        {"id": "engaged", "updated_at": stale},
                        {"id": "graded", "updated_at": stale},
                        {"id": "listed", "updated_at": fresh},
                        {"id": "noise", "updated_at": stale},
                    ]
                ],
                "user_jobs": [[{"job_posting_id": "engaged"}]],
                "scores": [[{"job_posting_id": "graded"}]],
            }
        )
        deleted, tombstoned = await archival._purge_old(sb, batch=500)

        # A: only the untouched+ungraded+delisted row is hard-deleted.
        assert deleted == 1
        deletes = sb.ops("jobs", "delete")
        assert len(deletes) == 1
        (del_in,) = deletes[0].called("in_")
        assert del_in[1] == ["noise"]

        # B: graded history + still-listed rows become tombstones with the
        # payload stripped; the engaged row is untouched in every form.
        assert tombstoned == 2
        updates = sb.ops("jobs", "update")
        assert len(updates) == 1
        (payload,) = updates[0].called("update")[0]
        assert payload["description_html"] is None
        assert "purged_at" in payload
        (ts_in,) = updates[0].called("in_")
        assert sorted(ts_in[1]) == ["graded", "listed"]
        assert "engaged" not in ts_in[1]

    @pytest.mark.asyncio
    async def test_engaged_rows_are_never_purged_in_any_form(self) -> None:
        sb = _FakeSupabase(
            {
                "jobs": [[{"id": "engaged", "updated_at": _days_ago(90)}]],
                "user_jobs": [[{"job_posting_id": "engaged"}]],
                "scores": [[]],
            }
        )
        deleted, tombstoned = await archival._purge_old(sb, batch=500)
        assert (deleted, tombstoned) == (0, 0)
        assert sb.ops("jobs", "delete") == []
        assert sb.ops("jobs", "update") == []


class TestRunArchivalSweep:
    @pytest.mark.asyncio
    async def test_zero_batch_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(archival.settings, "archival_sweep_batch", 0)
        sb = _FakeSupabase({})
        counts = await archival.run_archival_sweep(sb)
        assert counts == {"archived": 0, "deleted": 0, "tombstoned": 0}
        assert sb.log == []

    @pytest.mark.asyncio
    async def test_errors_are_swallowed(self) -> None:
        class _Boom:
            def table(self, name: str) -> Any:
                raise RuntimeError("db down")

        counts = await archival.run_archival_sweep(_Boom())
        assert counts == {"archived": 0, "deleted": 0, "tombstoned": 0}


class TestPollerPurgeGuard:
    @pytest.mark.asyncio
    async def test_drops_tombstoned_rows_before_upsert(self) -> None:
        sb = _FakeSupabase({"jobs": [[{"external_id": "dead-1"}]]})
        rows = [
            {"external_id": "dead-1", "title": "Zombie"},
            {"external_id": "live-1", "title": "Fresh"},
        ]
        kept = await poller_mod._drop_purged_rows(sb, "src-1", rows)
        assert [r["external_id"] for r in kept] == ["live-1"]

    @pytest.mark.asyncio
    async def test_fails_soft_on_error(self) -> None:
        class _Boom:
            def table(self, name: str) -> Any:
                raise RuntimeError("db down")

        rows = [{"external_id": "x"}]
        kept = await poller_mod._drop_purged_rows(_Boom(), "src-1", rows)
        assert kept == rows  # a resurrected tombstone beats a broken poll

    @pytest.mark.asyncio
    async def test_flag_off_skips_the_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            poller_mod.settings, "archival_sweep_enabled", False
        )
        called = False

        async def _fake_sweep(_sb: Any) -> dict[str, int]:
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(
            "app.services.archival.run_archival_sweep", _fake_sweep
        )
        poller_mod._ARCHIVAL_LAST_RUN = 0.0
        await poller_mod._maybe_run_archival_sweep(object())
        assert called is False

