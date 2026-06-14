"""BYOK key models (#5)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

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
