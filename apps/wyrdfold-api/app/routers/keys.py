"""BYOK per-user API key management — the Settings surface backend (#5 P4).

Write-only: the plaintext key is accepted on PUT and never returned; reads
expose only non-secret metadata (provider, ``last4``, timestamps). All
endpoints scope to the JWT subject.

These routes use the **service-role** client (``get_supabase``) on purpose:
``user_api_keys`` is service-role-only (RLS enabled, no authenticated
grants — the browser never touches the table), so the explicit ``user_id``
filter inside ``services.keys.store`` is the access control and is never
omitted. The router-level ``verify_supabase_jwt`` blocks the api-key
fallback, so a leaked operator key can't manage a user's keys here.

OpenRouter-only for v1 (#5 decision 1); other providers 400 until they earn
a per-user key story (Voyage stays on the instance key; Twilio with #13).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status
from supabase import Client

from app.dependencies import (
    get_current_user_id,
    get_supabase,
    verify_supabase_jwt,
)
from app.models.keys import (
    Provider,
    SetUserApiKeyRequest,
    UserApiKeyMeta,
    UserApiKeysResponse,
)
from app.services import keys

logger = logging.getLogger(__name__)

# v1 exposes only OpenRouter (the operator's single LLM service). Widen as
# other providers gain a per-user key story.
_V1_PROVIDERS: frozenset[str] = frozenset({"openrouter"})

router = APIRouter(
    prefix="/profile/keys",
    tags=["keys"],
    dependencies=[Depends(verify_supabase_jwt)],
)


def _require_v1_provider(provider: str) -> None:
    if provider not in _V1_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"BYOK is OpenRouter-only for now; '{provider}' is not supported.",
        )


def _require_byok_configured() -> None:
    if not keys.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This instance has no BYOK master key configured.",
        )


@router.get("", response_model=UserApiKeysResponse)
def list_keys(
    user_id: str = Depends(get_current_user_id),
    supabase: Client = Depends(get_supabase),
) -> UserApiKeysResponse:
    """Non-secret metadata for the caller's stored keys, plus whether BYOK
    is available on this instance (``BYOK_MASTER_KEY`` configured). When
    unavailable the key list is empty and the UI hides the fields."""
    available = keys.is_configured()
    meta = keys.list_key_meta(supabase, user_id=user_id) if available else []
    return UserApiKeysResponse(available=available, keys=meta)


@router.put("/{provider}", response_model=UserApiKeyMeta)
def put_key(
    body: SetUserApiKeyRequest,
    provider: Provider = Path(...),
    user_id: str = Depends(get_current_user_id),
    supabase: Client = Depends(get_supabase),
) -> UserApiKeyMeta:
    """Add or replace the caller's key for ``provider`` (write-only).

    Returns the new non-secret metadata. Replacing an existing key stamps
    ``rotated_at`` so the UI can show "rotated <date>"."""
    _require_v1_provider(provider)
    _require_byok_configured()

    key = body.key.strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Key must not be empty.",
        )

    # Rotation is cosmetic (drives the "added vs rotated" date in the UI);
    # this pre-read can race a concurrent set/delete but only mislabels,
    # never errors. The key write + the returned metadata are one atomic
    # upsert, so there's no read-back that a concurrent delete could defeat.
    existing = keys.list_key_meta(supabase, user_id=user_id)
    rotating = any(m.provider == provider for m in existing)
    return keys.set_key(
        supabase,
        user_id=user_id,
        provider=provider,
        plaintext=key,
        rotating=rotating,
    )


@router.delete("/{provider}")
def delete_key(
    provider: Provider = Path(...),
    user_id: str = Depends(get_current_user_id),
    supabase: Client = Depends(get_supabase),
) -> dict[str, bool]:
    """Remove the caller's key for ``provider``. Idempotent: deleting a key
    that isn't there returns ``{"deleted": false}`` rather than 404 — the
    desired end-state (no key) holds either way."""
    _require_v1_provider(provider)
    deleted = keys.delete_key(supabase, user_id=user_id, provider=provider)
    return {"deleted": deleted}
