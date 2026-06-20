"""BYOK keys: crypto + store round-trip (#5 P1).

Crypto tests pin the AES-256-GCM envelope contract (round-trip, tamper
detection, wrong-key rejection, master-key validation). Store tests run
set/get/rotate/delete against an in-memory fake that holds rows the same
way Postgres would, so a set→get only passes if encrypt → persist →
fetch → decrypt all compose correctly.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services import keys
from app.services.keys import crypto

# A valid (deterministic) 32-byte master key for tests.
_TEST_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "byok_master_key", _TEST_KEY_B64)


# ---- crypto -----------------------------------------------------------


def test_encrypt_decrypt_round_trip() -> None:
    plaintext = "sk-or-v1-abcdef0123456789"
    token = crypto.encrypt(plaintext)
    assert token != plaintext  # actually encrypted
    assert crypto.decrypt(token) == plaintext


def test_encrypt_is_nondeterministic() -> None:
    """Fresh nonce per call → same plaintext yields different ciphertext.
    Guards against accidentally dropping the random nonce."""
    a = crypto.encrypt("same-secret")
    b = crypto.encrypt("same-secret")
    assert a != b
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same-secret"


def test_decrypt_rejects_tampered_ciphertext() -> None:
    token = crypto.encrypt("sensitive")
    raw = bytearray(base64.b64decode(token))
    raw[-1] ^= 0x01  # flip a bit in the tag
    tampered = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(crypto.BYOKDecryptError):
        crypto.decrypt(tampered)


def test_decrypt_rejects_wrong_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = crypto.encrypt("secret")
    # Rotate the master key — old ciphertext must no longer decrypt.
    monkeypatch.setattr(
        settings, "byok_master_key", base64.b64encode(bytes(32)).decode("ascii")
    )
    with pytest.raises(crypto.BYOKDecryptError):
        crypto.decrypt(token)


def test_missing_master_key_raises_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "byok_master_key", "")
    assert crypto.is_configured() is False
    with pytest.raises(crypto.BYOKNotConfiguredError):
        crypto.encrypt("x")


def test_wrong_length_master_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings, "byok_master_key", base64.b64encode(b"tooshort").decode("ascii")
    )
    with pytest.raises(crypto.BYOKNotConfiguredError, match="32 bytes"):
        crypto.encrypt("x")


def test_last4() -> None:
    assert crypto.last4("sk-or-v1-abcd1234") == "1234"


# ---- in-memory fake supabase for the store ----------------------------


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

    def delete(self) -> _FakeQuery:
        self._op = "delete"
        return self

    def eq(self, col: str, val: Any) -> _FakeQuery:
        self._filters.append((col, val))
        return self

    def limit(self, _n: int) -> _FakeQuery:
        return self

    def order(self, _col: str) -> _FakeQuery:
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self) -> SimpleNamespace:
        if self._op == "upsert":
            assert self._row is not None
            keys_ = (self._conflict or "").split(",")
            for existing in self._store:
                if all(existing.get(k) == self._row.get(k) for k in keys_):
                    existing.update(self._row)
                    return SimpleNamespace(data=[existing])
            new = dict(self._row)
            new.setdefault("id", f"row-{len(self._store)}")
            new.setdefault("created_at", datetime.now(UTC).isoformat())
            new.setdefault("rotated_at", None)
            new.setdefault("last4", None)
            self._store.append(new)
            return SimpleNamespace(data=[new])
        if self._op == "select":
            cols = [c.strip() for c in (self._cols or "").split(",")]
            out = [
                {c: row.get(c) for c in cols}
                for row in self._store
                if self._matches(row)
            ]
            return SimpleNamespace(data=out)
        if self._op == "delete":
            removed = [r for r in self._store if self._matches(r)]
            self._store[:] = [r for r in self._store if not self._matches(r)]
            return SimpleNamespace(data=removed)
        raise AssertionError(f"unhandled op {self._op}")


class _FakeSupabase:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def table(self, name: str) -> _FakeQuery:
        assert name == keys.store.TABLE  # type: ignore[attr-defined]
        return _FakeQuery(self.rows)


# ---- store ------------------------------------------------------------


def test_set_then_get_round_trips_plaintext() -> None:
    sb = _FakeSupabase()
    keys.set_key(
        sb, user_id="u1", provider="openrouter", plaintext="sk-or-v1-secret9999"
    )
    assert keys.get_key(sb, user_id="u1", provider="openrouter") == "sk-or-v1-secret9999"


def test_stored_row_holds_ciphertext_not_plaintext() -> None:
    sb = _FakeSupabase()
    keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="sk-or-v1-plain")
    row = sb.rows[0]
    assert "sk-or-v1-plain" not in row["ciphertext"]
    assert row["last4"] == "lain"


def test_get_missing_returns_none() -> None:
    sb = _FakeSupabase()
    assert keys.get_key(sb, user_id="nobody", provider="openrouter") is None


def test_set_overwrites_in_place() -> None:
    sb = _FakeSupabase()
    keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="first-key")
    keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="second-key")
    assert len([r for r in sb.rows if r["user_id"] == "u1"]) == 1
    assert keys.get_key(sb, user_id="u1", provider="openrouter") == "second-key"


def test_two_users_isolated() -> None:
    sb = _FakeSupabase()
    keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="u1-key")
    keys.set_key(sb, user_id="u2", provider="openrouter", plaintext="u2-key")
    assert keys.get_key(sb, user_id="u1", provider="openrouter") == "u1-key"
    assert keys.get_key(sb, user_id="u2", provider="openrouter") == "u2-key"


def test_rotate_stamps_rotated_at() -> None:
    sb = _FakeSupabase()
    keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="old")
    assert sb.rows[0]["rotated_at"] is None
    keys.set_key(
        sb, user_id="u1", provider="openrouter", plaintext="new", rotating=True
    )
    assert sb.rows[0]["rotated_at"] is not None
    assert keys.get_key(sb, user_id="u1", provider="openrouter") == "new"


def test_set_key_returns_upserted_meta() -> None:
    # The router relies on this so it never re-reads to build its response
    # (a concurrent delete could empty that read and spuriously 500 — found
    # by P4 stress testing). set_key returns the upsert's own row metadata.
    sb = _FakeSupabase()
    meta = keys.set_key(
        sb, user_id="u1", provider="openrouter", plaintext="sk-or-v1-abcd"
    )
    assert meta.provider == "openrouter"
    assert meta.last4 == "abcd"
    assert meta.rotated_at is None

    rotated = keys.set_key(
        sb, user_id="u1", provider="openrouter", plaintext="sk-or-v1-wxyz",
        rotating=True,
    )
    assert rotated.last4 == "wxyz"
    assert rotated.rotated_at is not None


def test_delete_removes_row() -> None:
    sb = _FakeSupabase()
    keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="k")
    assert keys.delete_key(sb, user_id="u1", provider="openrouter") is True
    assert keys.get_key(sb, user_id="u1", provider="openrouter") is None
    assert keys.delete_key(sb, user_id="u1", provider="openrouter") is False


def test_list_key_meta_omits_ciphertext() -> None:
    sb = _FakeSupabase()
    keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="sk-or-v1-zzzz")
    meta = keys.list_key_meta(sb, user_id="u1")
    assert len(meta) == 1
    assert meta[0].provider == "openrouter"
    assert meta[0].last4 == "zzzz"
    # The model has no ciphertext field at all.
    assert not hasattr(meta[0], "ciphertext")


def test_set_rejects_unknown_provider() -> None:
    sb = _FakeSupabase()
    with pytest.raises(ValueError, match="unknown provider"):
        keys.set_key(sb, user_id="u1", provider="cohere", plaintext="x")  # type: ignore[arg-type]


def test_set_rejects_empty_key() -> None:
    sb = _FakeSupabase()
    with pytest.raises(ValueError, match="must not be empty"):
        keys.set_key(sb, user_id="u1", provider="openrouter", plaintext="   ")
