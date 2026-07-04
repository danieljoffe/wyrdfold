"""P-H3 (#192): provider SDK clients are pooled/reused across requests.

Rebuilding a provider client per request opened a fresh httpx pool + TLS
handshake each call. `get_default_client` / `get_client` now memoize real
clients by (api_key, timeout, max_retries); the mock stays fresh.
"""

from __future__ import annotations

import pytest

import app.services.llm as llm_mod
from app.config import settings
from app.services.embeddings import (
    MockEmbeddingsClient,
    VoyageEmbeddingsClient,
    reset_embeddings_client_cache,
)
from app.services.embeddings import (
    get_default_client as get_embeddings_client,
)
from app.services.llm import (
    MockLLMClient,
    OpenRouterLLMClient,
    get_client,
    get_default_client,
    reset_llm_client_cache,
)


def test_default_openrouter_client_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-env")
    reset_llm_client_cache()

    a = get_default_client()
    b = get_default_client()
    assert isinstance(a, OpenRouterLLMClient)
    assert a is b  # same pooled instance, not rebuilt per call


def test_mock_provider_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock holds no pool and tests want a fresh one — never cached."""
    monkeypatch.setattr(settings, "llm_provider", "mock")
    a = get_default_client()
    b = get_default_client()
    assert isinstance(a, MockLLMClient)
    assert a is not b


def test_byok_clients_keyed_by_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each distinct BYOK key gets its own reused client; the same key reuses."""
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-env")
    monkeypatch.setattr(settings, "byok_require_user_keys", False)
    reset_llm_client_cache()

    user_keys = {"u1": "sk-or-USER1", "u2": "sk-or-USER2"}
    monkeypatch.setattr(llm_mod, "_user_byok_key", lambda _sb, uid: user_keys.get(uid))

    c1a = get_client(object(), "u1")
    c1b = get_client(object(), "u1")
    c2 = get_client(object(), "u2")

    assert c1a is c1b  # same user key -> reused pool
    assert c1a is not c2  # different key -> distinct client
    # The right key is baked into each cached client (no cross-key bleed).
    assert c1a._client.api_key == "sk-or-USER1"
    assert c2._client.api_key == "sk-or-USER2"


def test_reset_rebuilds_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-env")
    reset_llm_client_cache()

    a = get_default_client()
    reset_llm_client_cache()
    b = get_default_client()
    assert a is not b  # cache cleared -> fresh instance


def test_embeddings_voyage_client_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embeddings_provider", "voyage")
    monkeypatch.setattr(settings, "voyage_api_key", "vk-env")
    reset_embeddings_client_cache()

    a = get_embeddings_client()
    b = get_embeddings_client()
    assert isinstance(a, VoyageEmbeddingsClient)
    assert a is b


def test_embeddings_mock_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embeddings_provider", "mock")
    a = get_embeddings_client()
    b = get_embeddings_client()
    assert isinstance(a, MockEmbeddingsClient)
    assert a is not b
