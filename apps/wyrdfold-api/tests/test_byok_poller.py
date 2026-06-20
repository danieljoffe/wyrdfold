"""BYOK #5 P3 — per-payer LLM client resolution in the background poller.

Unit coverage for ``poller._resolve_payer_client``: each payer's background
grading bills the payer's own OpenRouter key (``llm.get_client``), memoized
per payer, with a graceful ``None`` (defer) when a hosted require-mode payer
has no stored key.
"""

from unittest.mock import MagicMock

from app.services import poller as poller_mod
from app.services.llm import MissingUserKeyError


def test_resolve_payer_client_threads_supabase_and_payer(monkeypatch):
    seen: dict[str, object] = {}

    def fake_get_client(supabase, user_id):
        seen["supabase"] = supabase
        seen["user_id"] = user_id
        return MagicMock()

    monkeypatch.setattr(poller_mod, "get_llm_client", fake_get_client)

    sb = MagicMock()
    client = poller_mod._resolve_payer_client({}, sb, "payer-x")

    assert client is not None
    assert seen["supabase"] is sb
    assert seen["user_id"] == "payer-x"


def test_resolve_payer_client_memoizes_per_payer(monkeypatch):
    calls: list[str | None] = []

    def fake_get_client(_supabase, user_id):
        calls.append(user_id)
        return MagicMock(name=f"client-{user_id}")

    monkeypatch.setattr(poller_mod, "get_llm_client", fake_get_client)

    cache: dict[str | None, object] = {}
    sb = MagicMock()

    first = poller_mod._resolve_payer_client(cache, sb, "payer-a")
    again = poller_mod._resolve_payer_client(cache, sb, "payer-a")
    other = poller_mod._resolve_payer_client(cache, sb, "payer-b")

    # One resolution per distinct payer; same payer reuses the cached client
    # (one key decrypt, calls stay grouped on that payer's prompt cache).
    assert calls == ["payer-a", "payer-b"]
    assert again is first
    assert other is not first


def test_resolve_payer_client_defers_on_missing_key(monkeypatch):
    call_count = 0

    def fake_get_client(_supabase, _user_id):
        nonlocal call_count
        call_count += 1
        raise MissingUserKeyError("openrouter")

    monkeypatch.setattr(poller_mod, "get_llm_client", fake_get_client)

    cache: dict[str | None, object] = {}
    sb = MagicMock()
    first = poller_mod._resolve_payer_client(cache, sb, "no-key")
    second = poller_mod._resolve_payer_client(cache, sb, "no-key")

    # No key in require-mode → defer (None), never billing the operator key.
    assert first is None
    assert second is None
    # The None verdict is memoized — get_client isn't retried every call.
    assert call_count == 1


def test_resolve_payer_client_none_payer_uses_instance_key(monkeypatch):
    seen_user: list[str | None] = []

    def fake_get_client(_supabase, user_id):
        seen_user.append(user_id)
        return MagicMock(name="instance-client")

    monkeypatch.setattr(poller_mod, "get_llm_client", fake_get_client)

    # Unattributable background callers (payer None) resolve to the instance
    # key — unchanged from P2.
    client = poller_mod._resolve_payer_client({}, MagicMock(), None)

    assert client is not None
    assert seen_user == [None]
