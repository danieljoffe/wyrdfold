"""Unit tests for the in-flight analysis run registry (#459).

The router/task tests cover the happy transitions (begin → running → finish,
begin → fail → error). These pin the parts they don't: the lazy TTL sweep and
the per-user in-flight count that feeds the daily-count budget gate.
"""

from __future__ import annotations

import pytest

from app.services.analysis import run_registry


@pytest.fixture(autouse=True)
def _clean() -> None:
    run_registry.clear_all()


def _fake_clock(monkeypatch: pytest.MonkeyPatch, value: list[float]) -> None:
    """Drive run_registry's monotonic clock from ``value[0]`` so TTL sweeps are
    deterministic (no real sleeping)."""
    monkeypatch.setattr(run_registry.time, "monotonic", lambda: value[0])


def test_begin_running_and_finish() -> None:
    key = ("job", "tgt", "opt")
    assert run_registry.is_running(key) is False
    run_registry.begin(key, user_id="u1")
    assert run_registry.is_running(key) is True
    run_registry.finish(key)
    assert run_registry.get(key) is None


def test_error_is_not_running_so_a_retry_can_reclaim() -> None:
    key = ("job", "tgt", "opt")
    run_registry.begin(key, user_id="u1")
    run_registry.fail(key, message="boom")
    st = run_registry.get(key)
    assert st is not None and st.status == "error" and st.error == "boom"
    # An error is NOT "running", so a retry POST is allowed to re-claim it.
    assert run_registry.is_running(key) is False


def test_error_entry_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    _fake_clock(monkeypatch, clock)
    key = ("job", "tgt", "opt")
    run_registry.begin(key, user_id="u1")
    run_registry.fail(key, message="boom")
    assert run_registry.get(key) is not None

    # Just before the TTL: still present.
    clock[0] = 1000.0 + run_registry._ERROR_TTL_S - 1
    assert run_registry.get(key) is not None
    # Past the TTL: swept on the next read → a retry isn't shadowed.
    clock[0] = 1000.0 + run_registry._ERROR_TTL_S + 1
    assert run_registry.get(key) is None


def test_stale_running_entry_is_swept(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 'running' entry whose task died without clearing it is reaped past the
    running TTL, so the next poll reads idle and the client re-kicks."""
    clock = [0.0]
    _fake_clock(monkeypatch, clock)
    key = ("job", "tgt", "opt")
    run_registry.begin(key, user_id="u1")
    clock[0] = run_registry._RUNNING_TTL_S + 1
    assert run_registry.get(key) is None


def test_running_count_is_per_user() -> None:
    run_registry.begin(("j1", "t", "o"), user_id="u1")
    run_registry.begin(("j2", "t", "o"), user_id="u1")
    run_registry.begin(("j3", "t", "o"), user_id="u2")
    # A finished run drops out of the count.
    run_registry.begin(("j4", "t", "o"), user_id="u1")
    run_registry.finish(("j4", "t", "o"))

    assert run_registry.running_count_for_user("u1") == 2
    assert run_registry.running_count_for_user("u2") == 1
    assert run_registry.running_count_for_user(None) == 0

    # An errored run is not "running" and doesn't count toward the gate.
    run_registry.fail(("j2", "t", "o"), message="x")
    assert run_registry.running_count_for_user("u1") == 1
