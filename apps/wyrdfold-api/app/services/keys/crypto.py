"""AES-256-GCM envelope encryption for BYOK provider keys (#5).

Pure crypto — no DB. The master key comes from ``BYOK_MASTER_KEY``
(base64 of exactly 32 bytes). Each secret is sealed with a fresh random
96-bit nonce; the stored token is ``base64(nonce || ciphertext || tag)``.
GCM's auth tag means tampering or a wrong master key surfaces as a
decrypt error, never silent garbage.

The ``cryptography`` package is already on the tree via ``pyjwt[crypto]``
— no new dependency.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

# 96-bit nonce is the GCM-recommended size; 256-bit key = AES-256.
_NONCE_BYTES = 12
_KEY_BYTES = 32


class BYOKNotConfiguredError(RuntimeError):
    """Raised when a BYOK crypto op is attempted with no/invalid
    ``BYOK_MASTER_KEY``. Callers translate this into a clear operator
    error (self-host) or a 503 (hosted) rather than a raw stack trace."""


class BYOKDecryptError(RuntimeError):
    """Raised when ciphertext can't be decrypted — tampered data, a
    rotated/wrong master key, or corruption. Never leaks plaintext."""


def _load_master_key() -> bytes:
    raw = settings.byok_master_key
    if not raw:
        raise BYOKNotConfiguredError(
            "BYOK_MASTER_KEY is unset. Generate one with "
            "`openssl rand -base64 32` and set it in the API env."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise BYOKNotConfiguredError(
            "BYOK_MASTER_KEY is not valid base64."
        ) from exc
    if len(key) != _KEY_BYTES:
        raise BYOKNotConfiguredError(
            f"BYOK_MASTER_KEY must decode to exactly {_KEY_BYTES} bytes "
            f"(got {len(key)}). Use `openssl rand -base64 32`."
        )
    return key


def is_configured() -> bool:
    """True when a usable master key is present. Lets callers branch on
    BYOK availability without catching an exception."""
    try:
        _load_master_key()
        return True
    except BYOKNotConfiguredError:
        return False


def encrypt(plaintext: str) -> str:
    """Seal ``plaintext`` → ``base64(nonce || ct || tag)``."""
    import os

    key = _load_master_key()
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + sealed).decode("ascii")


def decrypt(token: str) -> str:
    """Reverse :func:`encrypt`. Raises ``BYOKDecryptError`` on any
    integrity/parse failure."""
    key = _load_master_key()
    try:
        blob = base64.b64decode(token, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise BYOKDecryptError("ciphertext is not valid base64") from exc
    if len(blob) <= _NONCE_BYTES:
        raise BYOKDecryptError("ciphertext too short to contain a nonce")
    nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, sealed, None).decode("utf-8")
    except InvalidTag as exc:
        raise BYOKDecryptError(
            "ciphertext failed authentication — wrong master key or tampered data"
        ) from exc


def last4(plaintext: str) -> str:
    """Non-secret tail for the settings UI. Short keys (shouldn't happen
    for real provider keys) return what's there rather than padding."""
    return plaintext[-4:]
