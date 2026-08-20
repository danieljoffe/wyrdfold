"""BYOK LLM client factory + DI threading (#5 P2).

Pins the resolution matrix of ``app.services.llm.get_client`` — the
decision of *whose* key pays for a given request — and the DI layer's
translation of a missing required key into an HTTP 402. The headline
invariants:

* mock mode never touches stored keys (tests / local stay hermetic);
* a stored user key is used over the instance key (their spend);
* single-tenant self-host (``BYOK_REQUIRE_USER_KEYS=false``) falls back
  to the operator env key untouched;
* hosted (``=true``) refuses to bill the house for a logged-in user with
  no key;
* api-key / cron callers (``user_id is None``) always use the instance
  key, even when user keys are required;
* an undecryptable stored key degrades to "no usable key", never a 500.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app import dependencies
from app.config import settings
from app.services import keys
from app.services.llm import (
    MissingUserKeyError,
    MockLLMClient,
    OpenRouterLLMClient,
    get_client,
)

_TEST_MASTER_KEY = base64.b64encode(bytes(range(32))).decode("ascii")


@pytest.fixture
def openrouter_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real provider so the factory exercises BYOK rather than short-
    circuiting to mock. Env key distinct from any user key under test."""
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-ENV")
    monkeypatch.setattr(settings, "byok_require_user_keys", False)


def _client_key(client: Any) -> str:
    """The api_key baked into the underlying SDK client."""
    return client._client.api_key


# ---- factory: mock mode is hermetic -----------------------------------


def test_mock_provider_ignores_byok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock short-circuits before any key lookup — a user + a stored key
    must NOT cause a real client (tests/local must never hit an API)."""
    monkeypatch.setattr(settings, "llm_provider", "mock")
    # Make a key lookup explode if reached, proving it isn't.
    monkeypatch.setattr(keys, "get_key", lambda *a, **k: pytest.fail("keys read in mock mode"))
    client = get_client(object(), "user-1")
    assert isinstance(client, MockLLMClient)


# ---- factory: user key wins -------------------------------------------


def test_user_key_used_when_present(monkeypatch: pytest.MonkeyPatch, openrouter_mode: None) -> None:
    monkeypatch.setattr(keys, "is_configured", lambda: True)
    monkeypatch.setattr(keys, "get_key", lambda *a, **k: "sk-or-USER")
    client = get_client(object(), "user-1")
    assert isinstance(client, OpenRouterLLMClient)
    assert _client_key(client) == "sk-or-USER"


# ---- factory: fallback vs require -------------------------------------


def test_falls_back_to_env_key_when_not_required(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    """Self-host posture: logged-in user, no stored key → operator env
    key, behavior unchanged."""
    monkeypatch.setattr(keys, "is_configured", lambda: True)
    monkeypatch.setattr(keys, "get_key", lambda *a, **k: None)
    client = get_client(object(), "user-1")
    assert isinstance(client, OpenRouterLLMClient)
    assert _client_key(client) == "sk-or-ENV"


def test_missing_user_key_raises_when_required(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    monkeypatch.setattr(settings, "byok_require_user_keys", True)
    monkeypatch.setattr(keys, "is_configured", lambda: True)
    monkeypatch.setattr(keys, "get_key", lambda *a, **k: None)
    with pytest.raises(MissingUserKeyError) as exc:
        get_client(object(), "user-1")
    assert exc.value.provider == "openrouter"


def test_api_key_caller_uses_instance_key_even_when_required(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    """No user identity (cron / poller / batch) → instance key, never a
    402, regardless of the require flag."""
    monkeypatch.setattr(settings, "byok_require_user_keys", True)
    monkeypatch.setattr(
        keys, "get_key", lambda *a, **k: pytest.fail("keys read for api-key caller")
    )
    client = get_client(object(), None)
    assert isinstance(client, OpenRouterLLMClient)
    assert _client_key(client) == "sk-or-ENV"


def test_no_supabase_skips_byok(monkeypatch: pytest.MonkeyPatch, openrouter_mode: None) -> None:
    """Pool unconfigured (mock/local) → BYOK skipped, env key used."""
    monkeypatch.setattr(settings, "byok_require_user_keys", True)
    monkeypatch.setattr(keys, "get_key", lambda *a, **k: pytest.fail("keys read with no supabase"))
    client = get_client(None, "user-1")
    assert isinstance(client, OpenRouterLLMClient)
    assert _client_key(client) == "sk-or-ENV"


# ---- factory: degradation is safe -------------------------------------


def test_byok_not_configured_skips_lookup(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    """No master key → no key read; falls back rather than erroring."""
    monkeypatch.setattr(keys, "is_configured", lambda: False)
    monkeypatch.setattr(keys, "get_key", lambda *a, **k: pytest.fail("get_key with no master key"))
    client = get_client(object(), "user-1")
    assert _client_key(client) == "sk-or-ENV"


def test_decrypt_error_treated_as_no_key(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    """A row that won't decrypt (rotated/wrong master key) must not 500 —
    it degrades to 'no usable key' (→ env fallback here)."""
    monkeypatch.setattr(keys, "is_configured", lambda: True)

    def _boom(*a: Any, **k: Any) -> str:
        raise keys.BYOKDecryptError("nope")

    monkeypatch.setattr(keys, "get_key", _boom)
    client = get_client(object(), "user-1")
    assert _client_key(client) == "sk-or-ENV"


def test_decrypt_error_then_required_raises(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    monkeypatch.setattr(settings, "byok_require_user_keys", True)
    monkeypatch.setattr(keys, "is_configured", lambda: True)

    def _boom(*a: Any, **k: Any) -> str:
        raise keys.BYOKDecryptError("nope")

    monkeypatch.setattr(keys, "get_key", _boom)
    with pytest.raises(MissingUserKeyError):
        get_client(object(), "user-1")


# ---- end-to-end through the real store + crypto -----------------------


async def test_end_to_end_stored_key_threads_through(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    """encrypt → persist → fetch → decrypt → client, with no mocking of
    the keys service — only the storage backend is faked."""
    monkeypatch.setattr(settings, "byok_master_key", _TEST_MASTER_KEY)
    sb = _FakeSupabase()
    await keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="sk-or-REAL42")
    client = get_client(sb, "u1")
    assert isinstance(client, OpenRouterLLMClient)
    assert _client_key(client) == "sk-or-REAL42"


# ---- DI layer: MissingUserKeyError → HTTP 402 -------------------------


async def test_get_llm_client_translates_missing_key_to_402(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    """The route-facing contract: a required-but-absent key surfaces as a
    402 'add your key', not the raw domain error or a 500. The dep is async
    now (#57 PR-G2e-8): the JWT sub resolves off-loop and the BYOK read runs on
    the async service client."""
    monkeypatch.setattr(settings, "byok_require_user_keys", True)
    monkeypatch.setattr(keys, "is_configured", lambda: True)

    async def _no_key(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr(keys, "get_key_async", _no_key)

    # Force a resolved user + a configured async pool without real JWT/Supabase.
    async def _sub(*a: object, **k: object) -> str:
        return "user-1"

    monkeypatch.setattr(dependencies, "_try_decode_jwt_sub_async", _sub)
    monkeypatch.setattr("app.supabase_pool.get_async_supabase", lambda: object())
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_llm_client(SimpleNamespace(), settings)
    assert exc.value.status_code == 402
    assert "key" in exc.value.detail.lower()


# ---- in-memory fake supabase (mirrors test_byok_keys.py) --------------


class _Result:
    """Dual result: the async ``set_key`` write path ``await``s ``execute()``
    while the still-sync ``get_key`` read path calls it directly (#57 slice 4)."""

    def __init__(self, data: Any) -> None:
        self.data = data

    def __await__(self):  # type: ignore[no-untyped-def]
        yield from ()
        return self


class _FakeQuery:
    def __init__(self, store: list[dict[str, Any]]) -> None:
        self._store = store
        self._op: str | None = None
        self._row: dict[str, Any] | None = None
        self._conflict: str | None = None
        self._cols: str | None = None
        self._filters: list[tuple[str, Any]] = []

    def upsert(self, row: dict[str, Any], on_conflict: str | None = None) -> _FakeQuery:
        self._op, self._row, self._conflict = "upsert", row, on_conflict
        return self

    def select(self, cols: str) -> _FakeQuery:
        self._op, self._cols = "select", cols
        return self

    def eq(self, col: str, val: Any) -> _FakeQuery:
        self._filters.append((col, val))
        return self

    def limit(self, _n: int) -> _FakeQuery:
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self) -> _Result:
        if self._op == "upsert":
            assert self._row is not None
            # Mimic PostgREST's RETURNING representation: the full row,
            # including the DB-default columns set_key doesn't send.
            stored = dict(self._row)
            stored.setdefault("created_at", "2026-01-01T00:00:00+00:00")
            stored.setdefault("rotated_at", None)
            self._store.append(stored)
            return _Result(data=[stored])
        if self._op == "select":
            cols = [c.strip() for c in (self._cols or "").split(",")]
            out = [{c: row.get(c) for c in cols} for row in self._store if self._matches(row)]
            return _Result(data=out)
        raise AssertionError(f"unhandled op {self._op}")


class _FakeSupabase:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def table(self, name: str) -> _FakeQuery:
        assert name == keys.store.TABLE  # type: ignore[attr-defined]
        return _FakeQuery(self.rows)


async def test_get_llm_client_translates_expired_trial_to_a_different_402(
    monkeypatch: pytest.MonkeyPatch, openrouter_mode: None
) -> None:
    """Both refusals are 402, but they must not say the same thing (#841).

    An expired trial user has no OpenRouter key and no reason to get one —
    telling them to add one is the dead end the trial exists to remove. The
    message must point at subscribing instead.
    """
    from app.services.llm import TrialExpiredError

    async def _raise_expired(*a: object, **k: object) -> None:
        raise TrialExpiredError()

    async def _sub(*a: object, **k: object) -> str:
        return "user-1"

    monkeypatch.setattr("app.services.llm.get_client_async", _raise_expired)
    monkeypatch.setattr(dependencies, "_try_decode_jwt_sub_async", _sub)
    monkeypatch.setattr("app.supabase_pool.get_async_supabase", lambda: object())

    with pytest.raises(HTTPException) as exc:
        await dependencies.get_llm_client(SimpleNamespace(), settings)

    assert exc.value.status_code == 402
    detail = exc.value.detail.lower()
    assert "trial" in detail and "subscribe" in detail
    # The precise failure this guards against: falling through to the BYOK
    # branch and telling a trial user to go and buy an API key.
    assert "openrouter" not in detail
