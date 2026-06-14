"""BYOK per-user provider API keys (#5).

`crypto` — AES-256-GCM envelope encryption (no DB).
`store` — encrypt-on-write / decrypt-on-read CRUD over `user_api_keys`.

Phase 1 ships storage + crypto only; the `get_client(user_id)` factory
that consumes these lands in P2.
"""

from app.services.keys.crypto import (
    BYOKDecryptError,
    BYOKNotConfiguredError,
    decrypt,
    encrypt,
    is_configured,
    last4,
)
from app.services.keys.store import (
    delete_key,
    get_key,
    list_key_meta,
    set_key,
)

__all__ = [
    "BYOKDecryptError",
    "BYOKNotConfiguredError",
    "decrypt",
    "delete_key",
    "encrypt",
    "get_key",
    "is_configured",
    "last4",
    "list_key_meta",
    "set_key",
]
