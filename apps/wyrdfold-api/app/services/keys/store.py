"""CRUD for per-user provider API keys (#5).

Thin wrapper over the ``user_api_keys`` table that encrypts on write and
decrypts on read via :mod:`app.services.keys.crypto`. Plaintext keys
exist only transiently in memory here and in the LLM-client factory that
consumes them — never persisted, never logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast, get_args

from supabase import Client

from app.models.keys import Provider, UserApiKeyMeta
from app.services.keys import crypto

TABLE = "user_api_keys"

_ALLOWED_PROVIDERS: frozenset[str] = frozenset(get_args(Provider))

# Columns safe to surface (no ciphertext).
_META_COLS = "provider, last4, created_at, updated_at, rotated_at"


def _validate_provider(provider: str) -> None:
    if provider not in _ALLOWED_PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r}; expected one of "
            f"{sorted(_ALLOWED_PROVIDERS)}"
        )


def set_key(
    supabase: Client,
    *,
    user_id: str,
    provider: Provider,
    plaintext: str,
    rotating: bool = False,
) -> UserApiKeyMeta:
    """Encrypt + upsert a user's key for ``provider``; return its metadata.

    ``rotating=True`` stamps ``rotated_at`` (a re-entered key replacing an
    old one); a first-time set leaves it NULL. Upsert keys on
    ``(user_id, provider)`` so re-setting overwrites in place.

    Returns the upserted row's non-secret :class:`UserApiKeyMeta` directly
    from the write's ``RETURNING`` representation. Callers must NOT re-read
    to build their response: a concurrent ``delete_key`` landing between
    the write and a separate read would make that read come back empty and
    spuriously 500 the request (found by P4 stress testing). The upsert's
    own returned row is immune — it's the row this statement just wrote.
    """
    _validate_provider(provider)
    if not plaintext.strip():
        raise ValueError("key must not be empty")

    now = datetime.now(UTC).isoformat()
    row: dict[str, Any] = {
        "user_id": user_id,
        "provider": provider,
        "ciphertext": crypto.encrypt(plaintext),
        "last4": crypto.last4(plaintext),
        "updated_at": now,
    }
    if rotating:
        row["rotated_at"] = now

    resp = (
        supabase.table(TABLE)
        .upsert(row, on_conflict="user_id,provider")
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        # PostgREST returns the upserted representation by default; an empty
        # body means a misconfigured deployment, not a concurrency race.
        raise RuntimeError("set_key: upsert returned no representation row")
    return UserApiKeyMeta.model_validate(rows[0])


def get_key(
    supabase: Client,
    *,
    user_id: str,
    provider: Provider,
) -> str | None:
    """Return the decrypted plaintext key, or None if the user hasn't set
    one for ``provider``. Raises ``BYOKDecryptError`` if a stored row
    can't be decrypted (rotated/wrong master key) — callers should treat
    that as "no usable key" but it's surfaced rather than swallowed so a
    misconfigured master key is loud."""
    _validate_provider(provider)
    resp = (
        supabase.table(TABLE)
        .select("ciphertext")
        .eq("user_id", user_id)
        .eq("provider", provider)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return crypto.decrypt(rows[0]["ciphertext"])


def list_key_meta(supabase: Client, *, user_id: str) -> list[UserApiKeyMeta]:
    """Non-secret metadata for every provider the user has a key for —
    the settings UI's read path. Never touches ciphertext."""
    resp = (
        supabase.table(TABLE)
        .select(_META_COLS)
        .eq("user_id", user_id)
        .order("provider")
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return [UserApiKeyMeta.model_validate(r) for r in rows]


def delete_key(
    supabase: Client,
    *,
    user_id: str,
    provider: Provider,
) -> bool:
    """Delete a user's key for ``provider``. Returns True if a row was
    removed, False if there was nothing to delete."""
    _validate_provider(provider)
    resp = (
        supabase.table(TABLE)
        .delete()
        .eq("user_id", user_id)
        .eq("provider", provider)
        .execute()
    )
    return bool(resp.data)
