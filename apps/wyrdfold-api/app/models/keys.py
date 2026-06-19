"""BYOK key models (#5)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Mirrors the CHECK constraint on user_api_keys.provider.
Provider = Literal["openrouter", "anthropic", "voyage", "twilio"]


class UserApiKeyMeta(BaseModel):
    """Non-secret view of a stored key — what the settings UI renders.

    Deliberately omits ciphertext: nothing outside the keys service ever
    needs the encrypted blob, and never the plaintext.
    """

    provider: Provider
    last4: str | None
    created_at: datetime
    updated_at: datetime
    rotated_at: datetime | None


class SetUserApiKeyRequest(BaseModel):
    """Write-only request body for storing a key (#5 P4).

    The plaintext is accepted here and never echoed back — responses only
    ever carry :class:`UserApiKeyMeta`.
    """

    key: str = Field(min_length=1, description="Provider API key (write-only).")


class UserApiKeysResponse(BaseModel):
    """The settings surface's read payload: the caller's stored-key metadata
    plus whether BYOK is available on this instance (a ``BYOK_MASTER_KEY``
    is configured). When ``available`` is false the instance runs on
    operator env keys and the UI hides the key fields."""

    available: bool
    keys: list[UserApiKeyMeta]
