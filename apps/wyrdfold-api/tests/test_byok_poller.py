"""BYOK #5 P3 — per-payer LLM client resolution in the background poller.

Unit coverage for ``poller._resolve_payer_client``: each payer's background
grading bills the payer's own OpenRouter key (``llm.get_client_async``), memoized
per payer, with a graceful ``None`` (defer) when a hosted require-mode payer
has no stored key. Async since #57 PR-G2e-1 — the key read awaits on the pooled
async service client.
"""

import logging
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services import poller as poller_mod
from app.services.llm import MissingUserKeyError


async def test_resolve_payer_client_threads_supabase_and_payer(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_get_client(supabase, user_id):
        seen["supabase"] = supabase
        seen["user_id"] = user_id
        return MagicMock()

    monkeypatch.setattr(poller_mod, "get_llm_client_async", fake_get_client)

    sb = MagicMock()
    client = await poller_mod._resolve_payer_client({}, sb, "payer-x")

    assert client is not None
    assert seen["supabase"] is sb
    assert seen["user_id"] == "payer-x"


async def test_resolve_payer_client_memoizes_per_payer(monkeypatch):
    calls: list[str | None] = []

    async def fake_get_client(_supabase, user_id):
        calls.append(user_id)
        return MagicMock(name=f"client-{user_id}")

    monkeypatch.setattr(poller_mod, "get_llm_client_async", fake_get_client)

    cache: dict[str | None, object] = {}
    sb = MagicMock()

    first = await poller_mod._resolve_payer_client(cache, sb, "payer-a")
    again = await poller_mod._resolve_payer_client(cache, sb, "payer-a")
    other = await poller_mod._resolve_payer_client(cache, sb, "payer-b")

    # One resolution per distinct payer; same payer reuses the cached client
    # (one key decrypt, calls stay grouped on that payer's prompt cache).
    assert calls == ["payer-a", "payer-b"]
    assert again is first
    assert other is not first


async def test_resolve_payer_client_defers_on_missing_key(monkeypatch):
    call_count = 0

    async def fake_get_client(_supabase, _user_id):
        nonlocal call_count
        call_count += 1
        raise MissingUserKeyError("openrouter")

    monkeypatch.setattr(poller_mod, "get_llm_client_async", fake_get_client)

    cache: dict[str | None, object] = {}
    sb = MagicMock()
    first = await poller_mod._resolve_payer_client(cache, sb, "no-key")
    second = await poller_mod._resolve_payer_client(cache, sb, "no-key")

    # No usable key → defer (None), never billing the operator key. Note the
    # trigger is an OR (flag OR a BYOK plan), so this defers regardless of
    # BYOK_REQUIRE_USER_KEYS — see the log-attribution test below (#841).
    assert first is None
    assert second is None
    # The None verdict is memoized — get_client_async isn't retried every call.
    assert call_count == 1


@pytest.mark.parametrize(
    ("flag", "expected", "forbidden"),
    [
        (False, "their plan requires BYOK", "BYOK_REQUIRE_USER_KEYS"),
        (True, "BYOK_REQUIRE_USER_KEYS is set", "their plan requires BYOK"),
    ],
    ids=["plan-branch", "flag-branch"],
)
async def test_defer_log_names_the_condition_that_actually_fired(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    flag: bool,
    expected: str,
    forbidden: str,
) -> None:
    """The defer reason must name which of the two triggers fired (#841).

    ``get_client_async`` raises on an OR: BYOK_REQUIRE_USER_KEYS, or a BYOK
    plan (the saas free tier). This log previously asserted the flag
    unconditionally, so on a hosted deployment — where the flag is unset and
    the plan branch fires — it named a variable that was not the cause. That
    is what sent #841 to the wrong conclusion.
    """

    async def fake_get_client(_supabase, _user_id):
        raise MissingUserKeyError("openrouter")

    monkeypatch.setattr(poller_mod, "get_llm_client_async", fake_get_client)
    monkeypatch.setattr(settings, "byok_require_user_keys", flag)

    # Precondition, so a silently-renamed setting can't make this vacuous.
    assert settings.byok_require_user_keys is flag

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=poller_mod.logger.name):
        assert await poller_mod._resolve_payer_client({}, MagicMock(), "payer-z") is None

    assert "Background grading deferred for payer payer-z" in caplog.text
    assert expected in caplog.text
    assert forbidden not in caplog.text


async def test_resolve_payer_client_none_payer_uses_instance_key(monkeypatch):
    seen_user: list[str | None] = []

    async def fake_get_client(_supabase, user_id):
        seen_user.append(user_id)
        return MagicMock(name="instance-client")

    monkeypatch.setattr(poller_mod, "get_llm_client_async", fake_get_client)

    # Unattributable background callers (payer None) resolve to the instance
    # key — unchanged from P2.
    client = await poller_mod._resolve_payer_client({}, MagicMock(), None)

    assert client is not None
    assert seen_user == [None]
