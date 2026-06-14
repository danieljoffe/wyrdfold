import hmac
import logging
from functools import lru_cache
from typing import TYPE_CHECKING

import jwt
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from jwt import PyJWKClient, PyJWKClientError
from supabase import Client

from app.config import Settings, settings

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.embeddings.client import EmbeddingsClient
    from app.services.llm.client import LLMClient

api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)

# Algorithms accepted for Supabase JWT verification. Supabase migrated to
# ES256 by default; older projects on RS256 still verify cleanly.
_JWT_ALGORITHMS = ["ES256", "RS256"]


def get_settings() -> Settings:
    return settings


def get_supabase() -> Client:
    from app.supabase_pool import get_supabase_pool

    client = get_supabase_pool()
    if client is None:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    return client


def get_user_supabase(
    request: Request,
    s: Settings = Depends(get_settings),
) -> Client:
    """Per-request Supabase client bound to the caller's JWT (#79).

    Unlike ``get_supabase`` (service-role, bypasses RLS), this routes
    queries through the user's token so Postgres RLS enforces per-user
    access. JWT-only: api-key/cron callers have no user token and must
    use ``get_supabase``. Not wired into any route yet — the per-user
    data paths migrate onto it table-by-table in later #79 phases.
    """
    if not s.supabase_url or not s.supabase_anon_key:
        raise HTTPException(
            status_code=503, detail="Supabase user client not configured"
        )
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing auth token")

    from app.supabase_pool import get_user_client

    return get_user_client(token)


def get_supabase_for_caller(
    request: Request,
    key: str | None = Security(api_key_header),
    s: Settings = Depends(get_settings),
) -> Client:
    """Pick the right Supabase client for a dual-auth route (#79 Phase 2).

    Mirrors the auth decision in ``get_current_user_id_optional`` so the
    client and the resolved user id always agree:

    * Valid JWT  -> per-request RLS-enforced user client (queries scoped
      by ``auth.uid() = user_id`` at Postgres, not just in Python).
    * API key    -> service-role client for the legacy single-tenant rows
      (``user_id IS NULL``); cron/poller/batch have no user token.

    A bearer token that fails verification falls through to the api-key
    check (and 401s if that also fails), matching the optional-user dep.
    """
    if s.supabase_url:
        token = _extract_bearer_token(request)
        if token:
            try:
                _decode_supabase_jwt(token, s)
            except HTTPException:
                logger.warning(
                    "auth_jwt_decode_failed path=%s reason=client_select",
                    request.url.path,
                )
            else:
                if not s.supabase_anon_key:
                    raise HTTPException(
                        status_code=503,
                        detail="Supabase user client not configured",
                    )
                from app.supabase_pool import get_user_client

                return get_user_client(token)
    if _api_key_matches(key, s.wyrdfold_api_key):
        return get_supabase()
    raise HTTPException(status_code=401, detail="Unauthorized")


def get_embeddings_client() -> "EmbeddingsClient":
    """Embeddings client factory. Returns the default (mock today,
    Voyage when the real implementation lands).
    """
    from app.services.embeddings import get_default_client

    return get_default_client()


def get_llm_client() -> "LLMClient":
    """LLM client factory. Returns the default (mock today, Anthropic
    when the real implementation lands).
    """
    from app.services.llm import get_default_client

    return get_default_client()


def _api_key_matches(presented: str | None, expected: str) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def verify_api_key(
    key: str | None = Security(api_key_header),
    s: Settings = Depends(get_settings),
) -> str:
    if not _api_key_matches(key, s.wyrdfold_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key or ""


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


def _jwks_url(s: Settings) -> str:
    return f"{s.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


def _issuer(s: Settings) -> str:
    return f"{s.supabase_url.rstrip('/')}/auth/v1"


@lru_cache(maxsize=4)
def _get_jwks_client_for_url(url: str) -> PyJWKClient:
    """LRU-cached PyJWKClient construction keyed by JWKS URL.

    `lru_cache(maxsize=4)` bounds memory in long-lived test processes
    that construct fresh `Settings` per run — without the bound, the
    cache would grow unbounded across `Settings` instances even though
    the URL set is tiny in practice (1–2 distinct values).

    PyJWKClient itself handles JWKS fetching, response caching (5-minute
    TTL by default), and per-key LRU caching internally. On unknown
    `kid` it auto-refreshes the JWKS and retries before raising — key
    rotation is transparent.
    """
    return PyJWKClient(
        url,
        cache_jwk_set=True,
        lifespan=300,
        max_cached_keys=16,
        timeout=10,
    )


def _get_jwks_client(s: Settings) -> PyJWKClient:
    return _get_jwks_client_for_url(_jwks_url(s))


def _decode_supabase_jwt(token: str, s: Settings) -> dict[str, object]:
    """Verify a Supabase access token against the project's JWKS.

    Raises ``HTTPException(401)`` on any verification failure. ``sub`` and
    ``exp`` are required so the token always identifies an account and a
    deadline. ``aud`` and ``iss`` claims are validated against the project.
    """
    try:
        signing_key = _get_jwks_client(s).get_signing_key_from_jwt(token)
    except (PyJWKClientError, jwt.PyJWTError) as exc:
        # PyJWKClientError: unknown kid (after refresh), JWKS unreachable,
        # malformed JWKS response. PyJWTError: malformed token (not enough
        # segments, bad base64, bad UTF-8 in header) — get_signing_key_from_jwt
        # parses the token header and re-raises decode errors. All collapse
        # to 401 — never leak parser/network detail to the client.
        raise HTTPException(status_code=401, detail="Invalid auth token") from exc

    try:
        payload: dict[str, object] = jwt.decode(
            token,
            signing_key.key,
            algorithms=_JWT_ALGORITHMS,
            options={"require": ["exp", "sub"]},
            audience=s.supabase_jwt_audience,
            issuer=_issuer(s),
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid auth token") from exc
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return payload


def verify_supabase_jwt(
    request: Request,
    s: Settings = Depends(get_settings),
) -> str:
    """Require a valid Supabase Bearer token. Returns the user's `sub`."""
    if not s.supabase_url:
        raise HTTPException(status_code=503, detail="JWT auth not configured")
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing auth token")
    payload = _decode_supabase_jwt(token, s)
    return str(payload["sub"])


def verify_api_key_or_jwt(
    request: Request,
    key: str | None = Security(api_key_header),
    s: Settings = Depends(get_settings),
) -> str:
    """Accept either the shared API key (cron) or a Supabase JWT (user).

    JWT decode failures are logged at WARNING (no token detail) so a
    spike of invalid tokens is detectable in observability — the
    previous silent swallow was a detection blind spot.
    """
    if _api_key_matches(key, s.wyrdfold_api_key):
        return "api-key"
    if s.supabase_url:
        token = _extract_bearer_token(request)
        if token:
            try:
                _decode_supabase_jwt(token, s)
            except HTTPException as exc:
                logger.warning(
                    "auth_jwt_decode_failed path=%s reason=%s",
                    request.url.path,
                    exc.detail,
                )
            else:
                return "jwt"
    raise HTTPException(status_code=401, detail="Unauthorized")


def _try_decode_jwt_sub(request: Request, s: Settings) -> str | None:
    """Return the JWT `sub` if a valid Bearer token is present, else None.

    Decode failures are logged at WARNING — see `verify_api_key_or_jwt`
    for rationale.

    On success, stamps the user id onto the current Sentry scope so any
    event captured later in the request carries the user (#26 F1). Without
    this, 500s on authenticated routes land in Sentry with no way to
    correlate to who hit them. The Sentry SDK no-ops when uninitialized,
    so this is cheap when no DSN is configured.
    """
    if not s.supabase_url:
        return None
    token = _extract_bearer_token(request)
    if not token:
        return None
    try:
        payload = _decode_supabase_jwt(token, s)
    except HTTPException as exc:
        logger.warning(
            "auth_jwt_decode_failed path=%s reason=%s",
            request.url.path,
            exc.detail,
        )
        return None
    sub = str(payload["sub"])
    try:
        import sentry_sdk

        sentry_sdk.set_user({"id": sub})
    except ImportError:  # pragma: no cover
        pass
    return sub


# Last write per user (time.monotonic seconds). In-process is fine on the
# single-replica deploy — worst case after a restart is one extra stamp.
_LAST_SEEN_STAMPED: dict[str, float] = {}
_LAST_SEEN_THROTTLE_S = 3600.0


def _touch_last_seen(user_id: str, s: Settings) -> None:
    """Fire-and-forget ``user_profiles.last_seen_at`` refresh.

    Drives the idle-account lifecycle (defer/deactivate). Throttled to
    one write per user per hour, and swallowed on any failure — activity
    tracking must never affect auth or add a failure mode to requests.
    """
    if not s.activity_tracking_enabled:
        return
    import time

    now = time.monotonic()
    last = _LAST_SEEN_STAMPED.get(user_id)
    if last is not None and now - last < _LAST_SEEN_THROTTLE_S:
        return
    _LAST_SEEN_STAMPED[user_id] = now
    try:
        from datetime import UTC, datetime

        get_supabase().table("user_profiles").update(
            {"last_seen_at": datetime.now(UTC).isoformat()}
        ).eq("user_id", user_id).execute()
    except Exception:
        logger.debug("last_seen stamp failed for %s", user_id, exc_info=True)


def get_current_user_id(
    request: Request,
    s: Settings = Depends(get_settings),
) -> str:
    """Return the JWT `sub` for the current request (JWT-required).

    Use on endpoints that need a real user identity (no api-key fallback).
    """
    sub = _try_decode_jwt_sub(request, s)
    if sub is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    _touch_last_seen(sub, s)
    return sub


def get_current_user_email(
    request: Request,
    s: Settings = Depends(get_settings),
) -> str | None:
    """Return the JWT `email` claim for the current request, if present.

    Best-effort — Supabase issues JWTs with the verified email in the
    ``email`` claim for password and magic-link sign-ins, but the claim
    is optional in the spec and may be missing for some auth providers
    (anonymous sign-ins, custom IdPs). Returns ``None`` when absent or
    when the token can't be decoded; callers should treat this as "we
    don't know the email" rather than an error.

    Used by the user-profile routes to pre-seed the ``email`` column on
    first-time profile creation, so onboarding's IdentityStep doesn't
    ask the user to retype the email they just signed in with.
    """
    if not s.supabase_url:
        return None
    token = _extract_bearer_token(request)
    if not token:
        return None
    try:
        payload = _decode_supabase_jwt(token, s)
    except HTTPException:
        return None
    email = payload.get("email")
    return email if isinstance(email, str) and email else None


def get_current_user_id_optional(
    request: Request,
    key: str | None = Security(api_key_header),
    s: Settings = Depends(get_settings),
) -> str | None:
    """Return the JWT `sub`, or None for api-key callers (cron/poller/batch).

    Routes accepting both auth modes use this to thread the user identity
    through the service layer: JWT → real per-user data, api-key → legacy
    single-tenant rows (where user_id IS NULL). Raises 401 if neither auth
    mode matches.
    """
    sub = _try_decode_jwt_sub(request, s)
    if sub is not None:
        _touch_last_seen(sub, s)
        return sub
    if _api_key_matches(key, s.wyrdfold_api_key):
        return None
    raise HTTPException(status_code=401, detail="Unauthorized")


def enforce_llm_budget(
    user_id: str | None = Depends(get_current_user_id_optional),
    supabase: Client = Depends(get_supabase),
    s: Settings = Depends(get_settings),
) -> None:
    """Defense-in-depth budget gate for LLM-touching routes.

    JWT users get checked against rolling hourly/daily/monthly cost caps
    from `llm_costs`. API-key callers (cron/poller/batch) bypass here —
    background work is charged to the target's activator and gated in
    the poller. Limits come from `user_llm_*_budget_usd` settings; the
    monthly cap honors the per-user `user_profiles` override; 0 disables.
    """
    if user_id is None:
        return
    from app.services.llm import budget

    monthly_cap, llm_enabled = budget.get_llm_account(
        supabase, user_id=user_id, default_usd=s.user_llm_monthly_budget_usd
    )
    budget.raise_if_llm_disabled(llm_enabled)
    budget.check_user_budget(
        supabase,
        user_id=user_id,
        daily_limit_usd=s.user_llm_daily_budget_usd,
        hourly_limit_usd=s.user_llm_hourly_budget_usd,
        monthly_limit_usd=monthly_cap,
    )


