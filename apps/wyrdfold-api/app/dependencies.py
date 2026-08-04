import asyncio
import hmac
import logging
import threading
from functools import lru_cache
from typing import TYPE_CHECKING

import jwt
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from jwt import PyJWK, PyJWKClient, PyJWKClientError
from supabase import AsyncClient

from app.background import spawn_detached
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


def get_async_service_supabase() -> AsyncClient:
    """Async service-role client (#57 slice 3) — the client for router paths
    that bypass RLS (cost ledger, shared-catalog reads, the LLM-budget gate).
    Returns the shared singleton (no await; created in the lifespan); 503 when
    unconfigured. The sync service client it once mirrored was deleted in
    PR-G2e-8 once the last three sync deps moved onto this one."""
    from app.supabase_pool import get_async_supabase

    client = get_async_supabase()
    if client is None:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    return client


async def get_async_user_supabase(
    request: Request,
    s: Settings = Depends(get_settings),
) -> AsyncClient:
    """Per-request, RLS-enforced user client on the pooled async HTTP/2 transport.

    Routes queries through the caller's JWT so Postgres RLS enforces per-user
    access (unlike ``get_async_service_supabase``, which is service-role and
    bypasses RLS). JWT-only: api-key/cron callers have no user token and must use
    the service-role client. This is the per-request user client for every RLS
    route since PR-F retired the sync per-request client (#57).
    """
    if not s.supabase_url or not s.supabase_anon_key:
        raise HTTPException(status_code=503, detail="Supabase user client not configured")
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing auth token")

    from app.supabase_pool import get_async_user_client

    return await get_async_user_client(token)


async def get_async_supabase_for_caller(
    request: Request,
    s: Settings = Depends(get_settings),
) -> AsyncClient:
    """Per-request RLS-enforced user client for a JWT caller (dual-auth routers).

    Scopes queries by ``auth.uid() = user_id`` at Postgres (not just in Python).
    JWT-only: since #192 the user-data gate rejects shared API keys, so this dep
    honours no key either — a leaked automation key can never obtain a
    service-role client on a user route. An absent/unverifiable token 401s (a JWT
    with no user client configured 503s — never a silent service-role fall-back
    that would bypass RLS). The JWT decode can trigger a blocking JWKS fetch
    (cold cache / unknown kid), so it runs in a worker thread — never on the
    event loop this async dependency executes on.
    """
    if s.supabase_url:
        token = _extract_bearer_token(request)
        if token:
            try:
                await asyncio.to_thread(_decode_supabase_jwt, token, s)
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
                from app.supabase_pool import get_async_user_client

                return await get_async_user_client(token)
    raise HTTPException(status_code=401, detail="Unauthorized")


def get_embeddings_client() -> "EmbeddingsClient":
    """Embeddings client factory. Returns the default (mock today,
    Voyage when the real implementation lands).
    """
    from app.services.embeddings import get_default_client

    return get_default_client()


async def get_llm_client(
    request: Request,
    s: Settings = Depends(get_settings),
) -> "LLMClient":
    """BYOK-aware LLM client for the request (#5 P2).

    Threads the caller's per-user OpenRouter key (when they've stored one)
    so their inference bills their key; otherwise falls back to the
    instance key, or — when ``BYOK_REQUIRE_USER_KEYS`` is set — 402s the
    request asking them to add one. See ``app.services.llm.get_client_async``.

    Auth is unchanged: the user is read opportunistically via the same
    non-raising JWT path as the optional-user dep — its CPU/verify (and any
    cold-cache JWKS fetch) run off the event loop in a worker thread
    (``asyncio.to_thread``, Perf-F1) — so a valid Bearer token identifies the
    user and api-key / cron callers resolve to None (and use the instance key).
    The BYOK key + plan reads run natively on the pooled async service client.
    A route's own auth dependency still governs access.
    """
    from app.services.llm import MissingUserKeyError, get_client_async
    from app.supabase_pool import get_async_supabase

    user_id = await _try_decode_jwt_sub_async(request, s)
    supabase = get_async_supabase()
    try:
        return await get_client_async(supabase, user_id)
    except MissingUserKeyError as exc:
        raise HTTPException(
            status_code=402,
            detail="Add your OpenRouter API key in Settings to use AI features.",
        ) from exc


def _api_key_matches(presented: str | None, expected: str) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def _operator_key_matches(presented: str | None, s: Settings) -> bool:
    """True if *presented* matches EITHER the legacy shared key or the
    dedicated cron/automation key (#29 round 3 / H4).

    Both checks run unconditionally (no short-circuit) so timing doesn't
    reveal which key — each underlying compare is already constant-time and
    no-ops when its expected value is unset.
    """
    legacy = _api_key_matches(presented, s.wyrdfold_api_key)
    cron = _api_key_matches(presented, s.wyrdfold_cron_key)
    return legacy or cron


def verify_api_key(
    key: str | None = Security(api_key_header),
    s: Settings = Depends(get_settings),
) -> str:
    # Strictly operator/cron routes accept EITHER the legacy
    # ``WYRDFOLD_API_KEY`` or the narrower ``WYRDFOLD_CRON_KEY`` (#29 R3 /
    # H4). The broad user-data acceptance in ``verify_api_key_or_jwt`` keeps
    # accepting only the legacy key, so the cron key can never authenticate
    # against user data.
    if not _operator_key_matches(key, s):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key or ""


def require_bff_secret(
    request: Request,
    s: Settings = Depends(get_settings),
) -> None:
    """Reject requests that didn't come through the trusted Next.js BFF (SEC-5).

    The public, IP-rate-limited endpoints (waitlist join, signup-mode) are only
    meant to be reached via the BFF, which injects the shared ``X-Wyrdfold-BFF``
    secret. Requiring it makes them BFF-only, so a direct hit to Railway can't
    spoof ``X-Forwarded-For`` to rotate past slowapi's per-IP limit — the
    residual gap left by ``FORWARDED_ALLOW_IPS="*"`` (the BFF already forwards
    the trusted Vercel ``x-real-ip`` as the peer, so legit per-client keying is
    unaffected).

    Fail-open when unconfigured: an empty ``wyrdfold_bff_secret`` skips the
    check, so a deploy that hasn't set the secret on BOTH the API (Railway) and
    the BFF (Vercel) yet won't hard-break public signup. Roll out by setting the
    BFF (Vercel) var first — it starts sending the header — then the API
    (Railway) var to begin enforcing, closing the window with no gap.
    """
    secret = s.wyrdfold_bff_secret
    if not secret:
        return
    provided = request.headers.get("x-wyrdfold-bff", "")
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=403, detail="Forbidden")


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


# ---- Warm JWKS cache for the on-loop rate-limit key_func --------------------
#
# slowapi's middleware calls the limiter key_func synchronously inside its async
# dispatch — on the event loop, on EVERY request. Decoding the JWT there via
# PyJWKClient can trigger a BLOCKING JWKS network fetch: on the 300s cache
# expiry, and — the attacker lever — on any token whose ``kid`` isn't cached
# (PyJWT force-refreshes on an unknown kid). A flood of garbage-kid tokens
# becomes a flood of blocking HTTPS round-trips serialized on the loop, stalling
# every in-flight request (hardening review 2026-07-21, Perf-F1).
#
# Fix: the key_func decodes against THIS warm, in-memory key set and NEVER
# fetches — an unknown kid just falls back to IP keying. It is pre-warmed at
# startup and refreshed out-of-band (``prewarm_and_start_jwks_refresher``). The
# authoritative route-dependency decode (``_decode_supabase_jwt``) is unchanged:
# it runs in FastAPI's threadpool, where a fetch is off-loop and safe.
_jwks_keys_lock = threading.Lock()
_jwks_keys: dict[str, PyJWK] = {}


def refresh_jwks_cache(s: Settings) -> int:
    """Blocking JWKS fetch → repopulate the warm key set. Returns the key count.

    Call from a worker thread (``asyncio.to_thread``), never on the event loop.
    """
    jwk_set = _get_jwks_client(s).get_jwk_set(refresh=True)
    fresh: dict[str, PyJWK] = {}
    for key in jwk_set.keys:
        kid = key.key_id
        if kid:
            fresh[kid] = key
    with _jwks_keys_lock:
        _jwks_keys.clear()
        _jwks_keys.update(fresh)
    return len(fresh)


def _decode_jwt_cache_only(token: str, s: Settings) -> dict[str, object] | None:
    """Verify a Supabase JWT against the warm key set ONLY — never fetches.

    Returns the claims on success; None on an unknown kid, a cold cache, or an
    invalid signature/claim. Same verification options as ``_decode_supabase_jwt``
    (algorithms, exp/sub required, audience + issuer), so a returned ``sub`` is
    authentic — the key_func can safely bucket by it.
    """
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None
    if not isinstance(kid, str) or not kid:
        return None
    with _jwks_keys_lock:
        signing_key = _jwks_keys.get(kid)
    if signing_key is None:
        return None
    try:
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=_JWT_ALGORITHMS,
            options={"require": ["exp", "sub"]},
            audience=s.supabase_jwt_audience,
            issuer=_issuer(s),
        )
    except jwt.PyJWTError:
        return None


def try_decode_jwt_sub_cache_only(request: Request, s: Settings) -> str | None:
    """``sub`` from a valid Bearer token, decoded against the warm key set only.

    For the rate-limit key_func, which runs on the event loop where a blocking
    JWKS fetch would stall all in-flight requests. An unknown kid / cold cache
    returns None so the caller IP-keys instead. Stamps the Sentry scope on
    success (mirrors ``_try_decode_jwt_sub_async``, #26 F1).
    """
    if not s.supabase_url:
        return None
    token = _extract_bearer_token(request)
    if not token:
        return None
    payload = _decode_jwt_cache_only(token, s)
    if payload is None:
        return None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    try:
        import sentry_sdk

        sentry_sdk.set_user({"id": sub})
    except ImportError:  # pragma: no cover
        pass
    return sub


# < PyJWKClient's 300s cache lifespan so the warm set never expires between
# refreshes; also picks up a Supabase signing-key rotation within this window.
_JWKS_REFRESH_INTERVAL_S = 240


async def _jwks_refresh_loop(s: Settings) -> None:
    while True:
        await asyncio.sleep(_JWKS_REFRESH_INTERVAL_S)
        try:
            n = await asyncio.to_thread(refresh_jwks_cache, s)
            logger.debug("jwks warm cache refreshed (%d keys)", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("jwks warm-cache refresh failed: %s: %s", type(exc).__name__, exc)


async def prewarm_and_start_jwks_refresher(s: Settings) -> "asyncio.Task[None] | None":
    """Pre-warm the JWKS key set and start the out-of-band refresher.

    Returns the refresher task (cancel it on shutdown) or None when Supabase
    isn't configured. A pre-warm failure is logged, not raised — the rate
    limiter degrades to IP keying until a refresh succeeds, and auth still works
    (the route dependency fetches in the threadpool).
    """
    if not s.supabase_url:
        return None
    try:
        n = await asyncio.to_thread(refresh_jwks_cache, s)
        logger.info("jwks warm cache pre-warmed (%d keys)", n)
    except Exception as exc:
        logger.warning(
            "jwks pre-warm failed (rate limiter IP-keys until refresh): %s: %s",
            type(exc).__name__,
            exc,
        )
    return asyncio.create_task(_jwks_refresh_loop(s))


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
    """Authenticate a user-data route with a Supabase JWT — JWT only.

    JWT decode failures are logged at WARNING (no token detail) so a
    spike of invalid tokens is detectable in observability — the
    previous silent swallow was a detection blind spot.

    NOTE (#29 R3 H4 / #192): this gate NO LONGER accepts any shared API key —
    neither the broad ``wyrdfold_api_key`` nor the ``wyrdfold_cron_key``. The
    six user-data routers (jobs / targets / tailor / experience / analysis /
    sources) are reachable only with a per-user JWT, so a leak of an
    automation key can no longer authenticate against user data. Shared keys
    remain valid ONLY on the strictly-operator gate (``verify_api_key``),
    which accepts both keys. Verified safe: the frontend BFF forwards user
    routes with the user's Bearer JWT (``lib/api/proxy.ts``) and never the
    broad key; operator/cron paths hit operator routes. Do not re-add any
    shared-key branch here.
    """
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


async def _try_decode_jwt_sub_async(request: Request, s: Settings) -> str | None:
    """Return the JWT `sub` if a valid Bearer token is present, else None.

    The async auth path (``get_current_user_id`` / ``_optional`` /
    ``get_llm_client``, #57 PR-G2e-8). Same result and side effects as the
    retired sync ``_try_decode_jwt_sub``: decode failures logged at WARNING
    (see ``verify_api_key_or_jwt`` for rationale), and the user id stamped onto
    the current Sentry scope on success (#26 F1) so any event captured later in
    the request carries the user (the SDK no-ops when uninitialized, so this is
    cheap when no DSN is configured).

    The signature verify — and any cold-cache / unknown-kid JWKS fetch — inside
    ``_decode_supabase_jwt`` run in a worker thread (``asyncio.to_thread``) so
    that CPU + potential blocking IO never lands on the event loop these
    ``async def`` deps run on (Perf-F1). The on-loop rate-limit key_func keeps
    its own never-fetch cache-only decoder (``try_decode_jwt_sub_cache_only``).
    """
    if not s.supabase_url:
        return None
    token = _extract_bearer_token(request)
    if not token:
        return None
    try:
        payload = await asyncio.to_thread(_decode_supabase_jwt, token, s)
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


def _should_stamp_last_seen(user_id: str, s: Settings) -> bool:
    """Throttle gate for the last-seen stamp: True at most once per user per
    hour (and never when activity tracking is disabled), stamping the in-process
    map as its side effect.

    Kept synchronous so the ``async def`` auth deps decide *without* awaiting
    whether to spawn the detached DB write — the ~one-write-per-user-per-hour
    throttle means almost every request short-circuits here with no round-trip.
    """
    if not s.activity_tracking_enabled:
        return False
    import time

    now = time.monotonic()
    last = _LAST_SEEN_STAMPED.get(user_id)
    if last is not None and now - last < _LAST_SEEN_THROTTLE_S:
        return False
    _LAST_SEEN_STAMPED[user_id] = now
    return True


async def _write_last_seen(user_id: str) -> None:
    """Best-effort ``user_profiles.last_seen_at`` UPDATE on the pooled async
    service client. Drives the idle-account lifecycle (defer/deactivate).

    Every failure is swallowed, and it no-ops when the async client isn't
    configured — activity tracking must never affect auth or add a request
    failure mode. Spawned DETACHED by the auth deps (``spawn_detached``) so the
    write never sits on the request's critical path (the sync per-request
    service client it used to write through was deleted in #57 PR-G2e-8).
    """
    try:
        from datetime import UTC, datetime

        from app.supabase_pool import get_async_supabase

        client = get_async_supabase()
        if client is None:
            return
        await (
            client.table("user_profiles")
            .update({"last_seen_at": datetime.now(UTC).isoformat()})
            .eq("user_id", user_id)
            .execute()
        )
    except Exception:
        logger.debug("last_seen stamp failed for %s", user_id, exc_info=True)


async def get_current_user_id(
    request: Request,
    s: Settings = Depends(get_settings),
) -> str:
    """Return the JWT `sub` for the current request (JWT-required).

    Use on endpoints that need a real user identity (no api-key fallback).
    The JWT verify runs off the event loop (``_try_decode_jwt_sub_async``); the
    best-effort last-seen stamp is spawned DETACHED so it never sits on the
    request's critical path (#57 PR-G2e-8).
    """
    sub = await _try_decode_jwt_sub_async(request, s)
    if sub is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if _should_stamp_last_seen(sub, s):
        spawn_detached(_write_last_seen(sub), name="last-seen-stamp")
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


async def get_current_user_id_optional(
    request: Request,
    key: str | None = Security(api_key_header),
    s: Settings = Depends(get_settings),
) -> str | None:
    """Return the JWT `sub`, or None for api-key callers (cron/poller/batch).

    Routes accepting both auth modes use this to thread the user identity
    through the service layer: JWT → real per-user data, api-key → legacy
    single-tenant rows (where user_id IS NULL). Raises 401 if neither auth
    mode matches. The JWT verify runs off the event loop
    (``_try_decode_jwt_sub_async``); the best-effort last-seen stamp is spawned
    DETACHED so it never sits on the request's critical path (#57 PR-G2e-8).
    """
    sub = await _try_decode_jwt_sub_async(request, s)
    if sub is not None:
        if _should_stamp_last_seen(sub, s):
            spawn_detached(_write_last_seen(sub), name="last-seen-stamp")
        return sub
    if _api_key_matches(key, s.wyrdfold_api_key):
        return None
    raise HTTPException(status_code=401, detail="Unauthorized")


async def enforce_llm_budget(
    user_id: str | None = Depends(get_current_user_id_optional),
    supabase: AsyncClient = Depends(get_async_service_supabase),
    s: Settings = Depends(get_settings),
) -> None:
    """Defense-in-depth budget gate for LLM-touching routes.

    JWT users get checked against rolling hourly/daily/monthly cost caps
    from `llm_costs`. API-key callers (cron/poller/batch) bypass here —
    background work is charged to the target's activator and gated in
    the poller. The monthly cap resolves per plan tier in saas mode
    (`budget.resolve_llm_quota_async`: BYOK payers skip it, managed tiers get
    the plan's interactive-only quota, per-user override wins); self_host
    keeps the settings default + override. 0 disables a window.

    Runs on the pooled async service client (#57 PR-G2e-8): the quota resolve
    and every spend/cap read are awaited natively instead of blocking a
    FastAPI threadpool worker. Same thresholds / tiers / window ordering as the
    sync gate it replaced.
    """
    if user_id is None:
        return
    from app.services.entitlements import NON_BILLABLE_PURPOSES
    from app.services.llm import budget

    quota = await budget.resolve_llm_quota_async(supabase, user_id=user_id)
    budget.raise_if_llm_disabled(quota.llm_enabled)
    await budget.check_user_budget_async(
        supabase,
        user_id=user_id,
        daily_limit_usd=s.user_llm_daily_budget_usd,
        hourly_limit_usd=s.user_llm_hourly_budget_usd,
        monthly_limit_usd=quota.monthly_cap_usd,
        monthly_excluded_purposes=quota.monthly_excluded_purposes,
        # The rails meter the user's own interactive spend; the ledger
        # attributes background catalog work to the target's payer, and
        # counting it here locked the owner out of interactive features
        # every day the pipeline ran (2026-07-13 incident).
        rail_excluded_purposes=NON_BILLABLE_PURPOSES,
    )
