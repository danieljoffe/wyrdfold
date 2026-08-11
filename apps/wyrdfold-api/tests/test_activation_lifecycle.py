"""Activation lifecycle: failure context + the stalled-row sweep (#557 §3, #649).

Pins three things:

* the failure-context INVARIANT — ``activation_error`` / ``activation_failed_at``
  are set only alongside ``activation_status='error'`` and cleared by every other
  transition, which is what makes re-activation a retry path;
* the sweep reclaims rows stranded in an IN-FLIGHT state (``deriving`` /
  ``polling``) to ``idle`` — never ``ready`` (that would claim work happened) and
  never ``error`` (that would blame the user for a backend stall);
* the sweep leaves resting states (``idle`` / ``ready`` / ``error``) and
  recently-touched in-flight rows alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.targets import TargetUpdate
from app.services.targets.activation import (
    IN_FLIGHT_STATUSES,
    ActivationError,
    is_user_actionable,
    sweep_stalled_activations,
)
from app.services.targets.crud import build_update_fields

pytestmark = pytest.mark.asyncio


# ---- failure-context invariant (#649) --------------------------------------


async def test_error_status_stamps_reason_and_timestamp() -> None:
    fields = build_update_fields(
        TargetUpdate(
            activation_status="error",
            activation_error=ActivationError.NO_EXPERIENCE_PROFILE,
        )
    )
    assert fields["activation_status"] == "error"
    assert fields["activation_error"] == "no_experience_profile"
    assert fields["activation_failed_at"] is not None


@pytest.mark.parametrize("status", ["idle", "deriving", "polling", "ready"])
async def test_every_non_error_transition_clears_the_failure_context(status: str) -> None:
    """THE retry path: re-activating a failed target wipes the old failure
    without any call site having to remember to."""
    fields = build_update_fields(TargetUpdate(activation_status=status))
    assert fields["activation_error"] is None
    assert fields["activation_failed_at"] is None


async def test_updates_that_do_not_touch_status_leave_the_failure_context_alone() -> None:
    """A label edit must not silently clear a real failure — None on the
    partial still means "don't touch the column"."""
    fields = build_update_fields(TargetUpdate(label="Staff Engineer"))
    assert "activation_error" not in fields
    assert "activation_failed_at" not in fields


async def test_user_actionable_classification() -> None:
    # The whole point of reason codes: "add your experience" is the user's to
    # fix; a pipeline blip is not.
    assert is_user_actionable(ActivationError.NO_EXPERIENCE_PROFILE)
    assert not is_user_actionable(ActivationError.PIPELINE_FAILED)
    assert not is_user_actionable(ActivationError.DERIVE_TIMEOUT)
    assert not is_user_actionable(None)


# ---- the sweep (#557 §3) ----------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: list[dict[str, Any]], log: list) -> None:
        self._rows = rows
        self._log = log
        self._payload: dict[str, Any] = {}
        self._eq: dict[str, Any] = {}
        self._lt: dict[str, Any] = {}

    def update(self, payload: dict[str, Any]) -> _FakeQuery:
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> _FakeQuery:
        self._eq[col] = val
        return self

    def lt(self, col: str, val: Any) -> _FakeQuery:
        self._lt[col] = val
        return self

    async def execute(self) -> Any:
        matched = [
            r
            for r in self._rows
            if all(r.get(c) == v for c, v in self._eq.items())
            and all(str(r.get(c)) < str(v) for c, v in self._lt.items())
        ]
        for row in matched:
            row.update(self._payload)
        self._log.append((dict(self._eq), dict(self._lt), dict(self._payload)))
        return type("Resp", (), {"data": matched})()


class _FakeSupabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.log: list = []

    def table(self, _name: str) -> _FakeQuery:
        return _FakeQuery(self.rows, self.log)


def _row(status: str, *, hours_ago: float, tid: str) -> dict[str, Any]:
    return {
        "id": tid,
        "activation_status": status,
        "updated_at": (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(),
    }


async def test_sweep_reclaims_stalled_in_flight_rows_to_idle() -> None:
    sb = _FakeSupabase(
        [
            _row("deriving", hours_ago=48, tid="stuck-deriving"),
            _row("polling", hours_ago=24 * 27, tid="stuck-polling-27d"),
        ]
    )
    reclaimed = await sweep_stalled_activations(sb, stale_after_hours=6)

    assert reclaimed == {"deriving": 1, "polling": 1}
    # Reclaimed to `idle` — the re-activatable state. NOT `ready` (no work was
    # done) and NOT `error` (the user did nothing wrong).
    assert {r["activation_status"] for r in sb.rows} == {"idle"}


@pytest.mark.parametrize("status", ["idle", "ready", "error"])
async def test_sweep_never_touches_resting_states(status: str) -> None:
    """`idle` in particular: it is a legitimate resting state (catalog targets
    live there permanently), not a stall."""
    sb = _FakeSupabase([_row(status, hours_ago=24 * 365, tid="ancient")])
    reclaimed = await sweep_stalled_activations(sb, stale_after_hours=6)

    assert reclaimed == {"deriving": 0, "polling": 0}
    assert sb.rows[0]["activation_status"] == status


async def test_sweep_leaves_a_live_activation_alone() -> None:
    """A pipeline that transitioned minutes ago is working, not stalled."""
    sb = _FakeSupabase([_row("polling", hours_ago=0.5, tid="live")])
    reclaimed = await sweep_stalled_activations(sb, stale_after_hours=6)

    assert reclaimed == {"deriving": 0, "polling": 0}
    assert sb.rows[0]["activation_status"] == "polling"


async def test_sweep_filters_on_status_and_age_together() -> None:
    """Both predicates must reach the DB — a status-only sweep would reclaim
    live pipelines, an age-only sweep would trample resting rows."""
    sb = _FakeSupabase([])
    await sweep_stalled_activations(sb, stale_after_hours=6)

    assert [eq["activation_status"] for eq, _, _ in sb.log] == list(IN_FLIGHT_STATUSES)
    for _, lt, payload in sb.log:
        assert "updated_at" in lt
        assert payload["activation_status"] == "idle"


async def test_sweep_is_idempotent() -> None:
    sb = _FakeSupabase([_row("polling", hours_ago=48, tid="stuck")])
    first = await sweep_stalled_activations(sb, stale_after_hours=6)
    second = await sweep_stalled_activations(sb, stale_after_hours=6)

    assert first == {"deriving": 0, "polling": 1}
    assert second == {"deriving": 0, "polling": 0}
