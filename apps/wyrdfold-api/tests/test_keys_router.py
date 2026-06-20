"""Tests for the BYOK key-management router (#5 P4).

The Settings surface backend: GET metadata + availability, PUT (write-only,
rotation-aware), DELETE (idempotent). Auth is overridden; the keys service
is monkeypatched so these assert the router's own logic — availability
gating, the OpenRouter-only v1 guard, rotation detection, user scoping —
not the crypto/store internals (covered by test_byok_keys.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    get_current_user_id,
    get_supabase,
    verify_supabase_jwt,
)
from app.main import app
from app.models.keys import UserApiKeyMeta


def _meta(provider: str = "openrouter", last4: str = "ab12") -> UserApiKeyMeta:
    now = datetime.now(UTC)
    return UserApiKeyMeta(
        provider=provider,  # type: ignore[arg-type]
        last4=last4,
        created_at=now,
        updated_at=now,
        rotated_at=None,
    )


@pytest.fixture
def client() -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: "user-1"
    app.dependency_overrides[verify_supabase_jwt] = lambda: "user-1"
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_get_lists_meta_when_available(client, monkeypatch):
    from app.routers import keys as router_mod

    monkeypatch.setattr(router_mod.keys, "is_configured", lambda: True)
    monkeypatch.setattr(
        router_mod.keys, "list_key_meta", lambda *_a, **_k: [_meta()]
    )

    resp = client.get("/profile/keys")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert len(body["keys"]) == 1
    assert body["keys"][0]["provider"] == "openrouter"
    assert body["keys"][0]["last4"] == "ab12"
    # Never leak ciphertext / plaintext.
    assert "ciphertext" not in body["keys"][0]
    assert "key" not in body["keys"][0]


def test_get_reports_unavailable_without_master_key(client, monkeypatch):
    from app.routers import keys as router_mod

    monkeypatch.setattr(router_mod.keys, "is_configured", lambda: False)
    called = MagicMock()
    monkeypatch.setattr(router_mod.keys, "list_key_meta", called)

    resp = client.get("/profile/keys")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["keys"] == []
    # Don't even touch the store when BYOK isn't configured.
    called.assert_not_called()


def test_put_new_key_sets_without_rotation(client, monkeypatch):
    from app.routers import keys as router_mod

    set_calls: list[dict] = []
    monkeypatch.setattr(router_mod.keys, "is_configured", lambda: True)
    # Rotation check sees nothing → first-time set.
    monkeypatch.setattr(router_mod.keys, "list_key_meta", lambda *_a, **_k: [])

    def _set_key(*_a, **k):
        set_calls.append(k)
        return _meta()  # the atomic upsert returns its own row metadata

    monkeypatch.setattr(router_mod.keys, "set_key", _set_key)

    resp = client.put("/profile/keys/openrouter", json={"key": "sk-or-newkey"})

    assert resp.status_code == 200
    assert resp.json()["provider"] == "openrouter"
    assert len(set_calls) == 1
    assert set_calls[0]["user_id"] == "user-1"
    assert set_calls[0]["provider"] == "openrouter"
    assert set_calls[0]["plaintext"] == "sk-or-newkey"
    assert set_calls[0]["rotating"] is False


def test_put_replacing_existing_key_marks_rotation(client, monkeypatch):
    from app.routers import keys as router_mod

    set_calls: list[dict] = []
    monkeypatch.setattr(router_mod.keys, "is_configured", lambda: True)
    monkeypatch.setattr(
        router_mod.keys, "list_key_meta", lambda *_a, **_k: [_meta()]
    )

    def _set_key(*_a, **k):
        set_calls.append(k)
        return _meta(last4="9999")

    monkeypatch.setattr(router_mod.keys, "set_key", _set_key)

    resp = client.put("/profile/keys/openrouter", json={"key": "sk-or-rotated"})

    assert resp.status_code == 200
    assert set_calls[0]["rotating"] is True


def test_put_503_when_byok_not_configured(client, monkeypatch):
    from app.routers import keys as router_mod

    monkeypatch.setattr(router_mod.keys, "is_configured", lambda: False)
    set_key = MagicMock()
    monkeypatch.setattr(router_mod.keys, "set_key", set_key)

    resp = client.put("/profile/keys/openrouter", json={"key": "sk-or-x"})

    assert resp.status_code == 503
    set_key.assert_not_called()


def test_put_400_for_non_openrouter_provider(client, monkeypatch):
    from app.routers import keys as router_mod

    monkeypatch.setattr(router_mod.keys, "is_configured", lambda: True)
    set_key = MagicMock()
    monkeypatch.setattr(router_mod.keys, "set_key", set_key)

    resp = client.put("/profile/keys/anthropic", json={"key": "sk-ant-x"})

    assert resp.status_code == 400
    set_key.assert_not_called()


def test_put_422_for_empty_key(client, monkeypatch):
    from app.routers import keys as router_mod

    monkeypatch.setattr(router_mod.keys, "is_configured", lambda: True)

    # Pydantic min_length rejects "" before the handler runs.
    resp = client.put("/profile/keys/openrouter", json={"key": ""})
    assert resp.status_code == 422


def test_put_422_for_whitespace_only_key(client, monkeypatch):
    from app.routers import keys as router_mod

    monkeypatch.setattr(router_mod.keys, "is_configured", lambda: True)
    monkeypatch.setattr(router_mod.keys, "list_key_meta", lambda *_a, **_k: [])
    set_key = MagicMock()
    monkeypatch.setattr(router_mod.keys, "set_key", set_key)

    resp = client.put("/profile/keys/openrouter", json={"key": "   "})

    assert resp.status_code == 422
    set_key.assert_not_called()


def test_delete_returns_true_when_removed(client, monkeypatch):
    from app.routers import keys as router_mod

    delete_calls: list[dict] = []

    def _delete(*_a, **k):
        delete_calls.append(k)
        return True

    monkeypatch.setattr(router_mod.keys, "delete_key", _delete)

    resp = client.delete("/profile/keys/openrouter")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert delete_calls[0]["user_id"] == "user-1"
    assert delete_calls[0]["provider"] == "openrouter"


def test_delete_idempotent_when_absent(client, monkeypatch):
    from app.routers import keys as router_mod

    monkeypatch.setattr(router_mod.keys, "delete_key", lambda *_a, **_k: False)

    resp = client.delete("/profile/keys/openrouter")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": False}


def test_delete_400_for_non_openrouter_provider(client, monkeypatch):
    from app.routers import keys as router_mod

    delete_key = MagicMock()
    monkeypatch.setattr(router_mod.keys, "delete_key", delete_key)

    resp = client.delete("/profile/keys/voyage")

    assert resp.status_code == 400
    delete_key.assert_not_called()
