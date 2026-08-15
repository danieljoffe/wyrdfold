"""Tests for the supabase-py retry helper."""

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.services.supabase_retry import (
    execute_with_retry,
    execute_with_retry_sync,
)


class _Counter:
    """Helper to build a sync function that fails N times then succeeds."""

    def __init__(self, fail_times: int, exc: Exception, success_value: Any = "ok"):
        self.fail_times = fail_times
        self.exc = exc
        self.success_value = success_value
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self.success_value


class _AsyncCounter:
    """Async callable that fails N times then succeeds — for the loop-native
    ``execute_with_retry`` (awaits the callable, no thread)."""

    def __init__(self, fail_times: int, exc: Exception, success_value: Any = "ok"):
        self.fail_times = fail_times
        self.exc = exc
        self.success_value = success_value
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self.success_value


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the retry backoff in tests so they don't burn wall-clock — both the
    sync ``time.sleep`` and the async ``asyncio.sleep`` paths."""
    import app.services.supabase_retry as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    async def _instant(_s: float) -> None:
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _instant)
    yield


def test_returns_value_on_first_success() -> None:
    fn = _Counter(fail_times=0, exc=httpx.RemoteProtocolError("x"))
    result = execute_with_retry_sync(fn, label="test")
    assert result == "ok"
    assert fn.calls == 1


def test_retries_then_succeeds_on_remote_protocol_error() -> None:
    fn = _Counter(fail_times=2, exc=httpx.RemoteProtocolError("disconnected"))
    result = execute_with_retry_sync(fn, label="test", retries=2)
    assert result == "ok"
    assert fn.calls == 3


def test_retries_on_connect_error() -> None:
    fn = _Counter(fail_times=1, exc=httpx.ConnectError("nope"))
    result = execute_with_retry_sync(fn, label="test", retries=1)
    assert result == "ok"
    assert fn.calls == 2


def test_retries_on_timeout_exception() -> None:
    fn = _Counter(fail_times=1, exc=httpx.TimeoutException("slow"))
    result = execute_with_retry_sync(fn, label="test", retries=1)
    assert result == "ok"
    assert fn.calls == 2


def test_raises_after_exhausting_retries() -> None:
    fn = _Counter(fail_times=99, exc=httpx.RemoteProtocolError("persistent"))
    with pytest.raises(httpx.RemoteProtocolError):
        execute_with_retry_sync(fn, label="test", retries=2)
    assert fn.calls == 3  # initial + 2 retries


def test_does_not_retry_on_http_status_error() -> None:
    """4xx/5xx from raise_for_status are protocol-level rejections — retrying
    a 422 won't unstick it. The helper should let those through unchanged."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = 422
    fn = _Counter(
        fail_times=99,
        exc=httpx.HTTPStatusError("bad", request=MagicMock(), response=response),
    )
    with pytest.raises(httpx.HTTPStatusError):
        execute_with_retry_sync(fn, label="test", retries=2)
    assert fn.calls == 1  # no retry


def test_does_not_retry_on_unrelated_exception() -> None:
    """A bug in the SQL builder (TypeError, ValueError, etc.) shouldn't
    get retried — the call is broken, not transient."""
    fn = _Counter(fail_times=99, exc=ValueError("broken"))
    with pytest.raises(ValueError):
        execute_with_retry_sync(fn, label="test", retries=2)
    assert fn.calls == 1


@pytest.mark.asyncio
async def test_async_retry_awaits_on_loop_and_retries() -> None:
    """Loop-native: awaits an async callable and retries transient blips with a
    non-blocking backoff (no executor thread — the #57 poll-write path)."""
    fn = _AsyncCounter(fail_times=1, exc=httpx.RemoteProtocolError("once"))
    result = await execute_with_retry(fn, label="async-test", retries=1)
    assert result == "ok"
    assert fn.calls == 2


@pytest.mark.asyncio
async def test_async_retry_raises_after_exhaustion() -> None:
    fn = _AsyncCounter(fail_times=99, exc=httpx.RemoteProtocolError("persistent"))
    with pytest.raises(httpx.RemoteProtocolError):
        await execute_with_retry(fn, label="async-test", retries=2)
    assert fn.calls == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_async_retry_does_not_retry_unrelated_exception() -> None:
    """A builder bug (ValueError) is not transient — surface it immediately."""
    fn = _AsyncCounter(fail_times=99, exc=ValueError("broken"))
    with pytest.raises(ValueError):
        await execute_with_retry(fn, label="async-test", retries=2)
    assert fn.calls == 1


# ---------------------------------------------------------------------------
# Statement-timeout (57014) opt-in retry — #604. The 2026-08-05 drive caught
# /insights/targets and the cross-target jobs fallback dying unhandled on a
# single 57014 while the identical read succeeded moments later.
# ---------------------------------------------------------------------------


def _statement_timeout() -> Exception:
    from postgrest.exceptions import APIError

    return APIError(
        {
            "message": "canceling statement due to statement timeout",
            "code": "57014",
            "hint": None,
            "details": None,
        }
    )


def test_is_statement_timeout_predicate() -> None:
    from postgrest.exceptions import APIError

    from app.services.supabase_retry import is_statement_timeout

    assert is_statement_timeout(_statement_timeout()) is True
    assert (
        is_statement_timeout(
            APIError({"message": "dup", "code": "23505", "hint": None, "details": None})
        )
        is False
    )
    assert is_statement_timeout(RuntimeError("57014")) is False


@pytest.mark.asyncio
async def test_async_statement_timeout_not_retried_by_default() -> None:
    from postgrest.exceptions import APIError

    counter = _AsyncCounter(fail_times=1, exc=_statement_timeout())
    with pytest.raises(APIError):
        await execute_with_retry(counter, label="t")
    assert counter.calls == 1


@pytest.mark.asyncio
async def test_async_statement_timeout_retried_when_opted_in() -> None:
    counter = _AsyncCounter(fail_times=1, exc=_statement_timeout())
    result = await execute_with_retry(counter, label="t", retry_statement_timeout=True)
    assert result == "ok"
    assert counter.calls == 2


@pytest.mark.asyncio
async def test_async_statement_timeout_exhaust_reraises() -> None:
    from postgrest.exceptions import APIError

    counter = _AsyncCounter(fail_times=99, exc=_statement_timeout())
    with pytest.raises(APIError):
        await execute_with_retry(counter, label="t", retries=2, retry_statement_timeout=True)
    assert counter.calls == 3


@pytest.mark.asyncio
async def test_async_opt_in_still_ignores_other_apierrors() -> None:
    from postgrest.exceptions import APIError

    counter = _AsyncCounter(
        fail_times=1,
        exc=APIError({"message": "dup", "code": "23505", "hint": None, "details": None}),
    )
    with pytest.raises(APIError):
        await execute_with_retry(counter, label="t", retry_statement_timeout=True)
    assert counter.calls == 1


def test_sync_statement_timeout_retried_only_when_opted_in() -> None:
    from postgrest.exceptions import APIError

    from app.services.supabase_retry import execute_with_retry_sync

    default = _Counter(fail_times=1, exc=_statement_timeout())
    with pytest.raises(APIError):
        execute_with_retry_sync(default, label="t")
    assert default.calls == 1

    opted = _Counter(fail_times=1, exc=_statement_timeout())
    assert execute_with_retry_sync(opted, label="t", retry_statement_timeout=True) == "ok"
    assert opted.calls == 2
