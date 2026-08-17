import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Receive, Scope, Send

from app.config import Settings, settings
from app.dependencies import prewarm_and_start_jwks_refresher
from app.http_client import close_http_client, close_safe_http_client
from app.logging_config import init_logging
from app.observability import init_sentry
from app.rate_limit import limiter
from app.routers import (
    admin,
    analysis,
    billing,
    discovery,
    experience,
    feedback,
    insights,
    job_search,
    jobs,
    keys,
    poll,
    public_search,
    search_events,
    sources,
    status,
    tailor,
    target_membership,
    targets,
    user_profile,
    waitlist,
)
from app.scheduler import start_scheduler_if_enabled
from app.services.llm.cost_log_buffer import buffer as cost_log_buffer
from app.services.llm.errors import LLMServiceError
from app.services.owner_provisioning import provision_owner
from app.services.search_events import buffer as search_events_buffer
from app.supabase_pool import (
    close_async_supabase,
    close_async_user_client,
    get_async_supabase,
    init_async_supabase,
)

_log = logging.getLogger("app")

# Wire JSON logging before Sentry init so any boot-time errors land in
# the configured format. No-op when LOG_FORMAT=text (the default).
init_logging(settings.log_format)
init_sentry()


def _validate_settings(s: Settings) -> None:
    """Fail fast on missing/invalid required settings.

    Called from within ``lifespan`` so the check runs at app startup
    rather than at module import — keeps tests/import order decoupled.
    """
    if not s.allowed_hosts_list:
        raise RuntimeError(
            "ALLOWED_HOSTS must be set (comma-separated host allowlist). Use '*' only in local dev."
        )

    # Without Supabase wiring, every authenticated request 503s with no
    # signal that the misconfig is the cause — the healthcheck stays
    # green (it doesn't touch the DB). Fail the lifespan instead so a
    # self-hoster's deploy log makes the cause obvious. #30 F2.
    if not s.supabase_url or not s.supabase_service_role_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set. "
            "Get them from the Supabase dashboard → Settings → API."
        )

    # The per-user RLS routes (experience, feedback, tailor, profile — #79)
    # build a request-scoped client from the caller's JWT + the anon key. With
    # it unset, every authenticated request 503s ("Supabase user client not
    # configured") while the healthcheck stays green — a prod outage that boots
    # clean and fails silently per request. Fail the lifespan instead so the
    # deploy log names the cause.
    if not s.supabase_anon_key:
        raise RuntimeError(
            "SUPABASE_ANON_KEY must be set (the anon/publishable key). The "
            "per-user RLS routes build a JWT-bound client from it; without it "
            "every authenticated request 503s. Supabase dashboard → Settings → API."
        )

    # If the operator selected the real Anthropic provider but didn't
    # configure a key, every LLM-backed request will 500 mid-call with
    # an opaque SDK ``TypeError`` ("Could not resolve authentication
    # method") — and nothing surfaces that misconfig until the first
    # user tries to onboard, derive a target, score a job, etc. Fail
    # the lifespan instead so the deploy logs make the cause obvious.
    if s.llm_provider == "anthropic" and not s.anthropic_api_key:
        raise RuntimeError(
            "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset. "
            "Either set ANTHROPIC_API_KEY or switch LLM_PROVIDER=mock."
        )

    # Same shape for Voyage embeddings — when the real provider is
    # selected without a key, embedding generation will explode partway
    # through a request flow (re-derive, conversation, etc.).
    if s.embeddings_provider == "voyage" and not s.voyage_api_key:
        raise RuntimeError(
            "EMBEDDINGS_PROVIDER=voyage but VOYAGE_API_KEY is unset. "
            "Either set VOYAGE_API_KEY or switch EMBEDDINGS_PROVIDER=mock."
        )

    # SEC-5: in saas mode the public waitlist / signup-mode endpoints are
    # internet-facing and rate-limited per client IP. Without the BFF shared
    # secret, a direct hit to the API can spoof X-Forwarded-For to rotate past
    # that limit. Warn rather than fail — ``require_bff_secret`` fails open by
    # design so setting it is a two-platform rollout, not a boot precondition —
    # so the deploy log flags the gap until it's set on Railway + Vercel.
    if s.deployment_mode == "saas" and not s.wyrdfold_bff_secret:
        _log.warning(
            "WYRDFOLD_BFF_SECRET is unset in saas mode: the public "
            "waitlist/signup-mode endpoints are reachable directly and their "
            "per-IP rate limit can be bypassed via X-Forwarded-For (SEC-5). "
            "Set it on both the API (Railway) and the BFF (Vercel) to enforce."
        )


_LEGACY_KEY_DISABLED_SIGNATURE = "Legacy API keys are disabled"


async def _probe_supabase_keys(
    s: Settings,
    *,
    fetch: Callable[[str, str], Awaitable[str]] | None = None,
) -> None:
    """Boot-time probe for known-bad Supabase keys.

    ``_validate_settings`` proves the keys are present; this additionally
    detects the one deterministic failure we can identify from a gateway
    response — the disabled-legacy-key signature — and boot-fails on it.
    It does NOT prove a key is otherwise valid (a wrong-but-enabled key
    still surfaces on the first real request). A key can be set yet
    **disabled** — Supabase is sunsetting the
    legacy anon/service_role JWT keys, and a disabled key makes every request
    through it fail. On 2026-07-02 the ``GET /jobs`` path flipped onto the RLS
    user client (built from the anon key), whose prod anon key was a disabled
    legacy key — 500-storming the hottest endpoint with no boot-time signal.
    This turns that class of misconfig into a clear boot failure.

    Only the deterministic disabled-key signature fails the boot; a network blip
    reaching Supabase warns and continues, so a transient outage can't keep the
    app from starting. A legacy JWT-format key (not yet disabled) warns.
    """
    if not s.supabase_url:
        return

    async def _default_fetch(url: str, key: str) -> str:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers={"apikey": key})
            return resp.text

    do_fetch = fetch or _default_fetch
    url = f"{s.supabase_url.rstrip('/')}/rest/v1/"
    for name, key in (
        ("SUPABASE_ANON_KEY", s.supabase_anon_key),
        ("SUPABASE_SERVICE_ROLE_KEY", s.supabase_service_role_key),
    ):
        if not key:
            continue
        if key.startswith("eyJ"):
            _log.warning(
                "%s is a legacy JWT-format Supabase key — these are being sunset; "
                "rotate to a publishable (anon) / secret (service-role) key (sb_...) "
                "before Supabase disables it.",
                name,
            )
        try:
            body = await do_fetch(url, key)
        except httpx.HTTPError as exc:
            _log.warning(
                "Supabase key liveness probe for %s could not reach %s (%r); "
                "skipping — a real key problem will still surface on the first "
                "authenticated request.",
                name,
                url,
                exc,
            )
            continue
        if _LEGACY_KEY_DISABLED_SIGNATURE in body:
            raise RuntimeError(
                f"{name} is a DISABLED legacy Supabase key: the API gateway rejects "
                f"it ('{_LEGACY_KEY_DISABLED_SIGNATURE}'), so every request using it "
                f"fails. Rotate to the new publishable (anon) / secret (service-role) "
                f"key in the Supabase dashboard -> Settings -> API and update the "
                f"deploy env. Boot-failing on purpose so the deploy log names the cause."
            )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _validate_settings(settings)
    # Skipped under the test flag (no test boots the lifespan; belt-and-braces
    # so a future `with TestClient(app)` test can't hit the network).
    if os.environ.get("WYRDFOLD_API_TESTING") != "1":
        await _probe_supabase_keys(settings)
    # Async service-role client (#57): the single pooled service-role client
    # every service-role path awaits — background work, owner provisioning, the
    # cost/search-event buffers, the scheduler discovery-catchup, ``/ready``
    # below, and (since PR-G2e-8) the last interactive deps too:
    # ``enforce_llm_budget``, ``get_llm_client``, and ``get_current_user_id`` →
    # the detached last-seen stamp. There is no sync service-role client anymore
    # (its JWKS decode stays off the loop via ``asyncio.to_thread`` instead,
    # Perf-F1). No-op when Supabase isn't configured.
    await init_async_supabase()
    # Self-host first-run: idempotently create the OWNER_EMAIL auth user so a
    # fresh instance is sign-in-able without dashboard work (Phase 2; no-op in
    # saas mode, when OWNER_EMAIL is unset, or when the owner already exists).
    # Runs on the async service client (#57 — GoTrue admin create_user awaited).
    # Same test-flag guard as the key probe above: provisioning hits the
    # auth admin API — a future `with TestClient(app)` test must not create
    # real users or need a live stack.
    supabase_for_owner = get_async_supabase()
    if supabase_for_owner is not None and os.environ.get("WYRDFOLD_API_TESTING") != "1":
        await provision_owner(supabase_for_owner, settings)
    scheduler = start_scheduler_if_enabled()
    # Pre-warm the JWKS key set and start its out-of-band refresher, so the
    # rate-limit key_func (which runs on the event loop) never blocks on a JWKS
    # fetch (Perf-F1). Same test-flag guard as the other network-touching
    # startup steps — no test boots the lifespan or should hit auth's network.
    jwks_refresher = None
    if os.environ.get("WYRDFOLD_API_TESTING") != "1":
        jwks_refresher = await prewarm_and_start_jwks_refresher(settings)
    # Background cost-log flush task. Cron paths enqueue rows and the
    # buffer drains them in a single bulk INSERT every few seconds, awaited
    # on the pooled async service client (#57). Started only when supabase is
    # configured (otherwise enqueued rows would accumulate forever in
    # tests/local dev without a backing DB).
    supabase_for_buffer = get_async_supabase()
    if supabase_for_buffer is not None:
        cost_log_buffer.start(supabase_for_buffer)
        # Search-funnel metrics ride the same buffered-INSERT machinery
        # (#467 §10 PR6) — a second instance pointed at search_events.
        search_events_buffer.start(supabase_for_buffer)
    try:
        yield
    finally:
        if jwks_refresher is not None:
            jwks_refresher.cancel()
            await asyncio.gather(jwks_refresher, return_exceptions=True)
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if supabase_for_buffer is not None:
            await cost_log_buffer.stop(supabase_for_buffer)
            await search_events_buffer.stop(supabase_for_buffer)
        await close_async_supabase()
        await close_async_user_client()
        await close_http_client()
        await close_safe_http_client()


app = FastAPI(
    title="WyrdFold API",
    description="WyrdFold backend — polls Greenhouse boards, scores postings, serves results",
    version="0.1.0",
    lifespan=lifespan,
)

# Rate limiting (slowapi). State attachment is required for the middleware
# and decorator to find the shared limiter; the exception handler converts
# RateLimitExceeded into a clean JSON 429 instead of slowapi's default
# plain-text response. See ``app/rate_limit.py`` for key strategy.
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    _log.info(
        "rate_limit_exceeded path=%s detail=%s",
        request.url.path,
        exc.detail,
    )
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded. Slow down and try again shortly.",
            "limit": str(exc.detail),
        },
        headers={"Retry-After": "60"},
    )


app.add_middleware(SlowAPIMiddleware)


_PROBE_PATHS = frozenset({"/health", "/ready"})


class _HealthBypassTrustedHost(TrustedHostMiddleware):
    """Skip host validation for infrastructure health/readiness probes.

    Railway's load balancer and the Docker HEALTHCHECK hit these by IP,
    not by the public Host header, so they'd be rejected by the host
    allowlist otherwise.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in _PROBE_PATHS:
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app.add_middleware(
    _HealthBypassTrustedHost,
    allowed_hosts=settings.allowed_hosts_list,
)

# Compress JSON responses ≥1KB. List endpoints can return hundreds of jobs;
# gzip cuts ~70-80% off typical JSON payloads.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Explicit CORS allowlist (Phase 5 P1-Sec). Empty = no browser-direct
# callers — the Next.js app proxies via server-side fetch and doesn't need
# CORS. Set CORS_ALLOWED_ORIGINS in env when adding browser callers.
if settings.cors_allowed_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins_list,
        allow_credentials=False,  # we use Bearer JWT, not cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type", "x-api-key"],
        max_age=600,
    )


@app.middleware("http")
async def _log_slow_requests(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Log requests slower than ``settings.slow_request_threshold_ms``.

    Adds an ``X-Response-Time-Ms`` header on every response so callers can
    correlate without parsing logs.
    """
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000.0

    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"

    if duration_ms >= settings.slow_request_threshold_ms:
        _log.warning(
            "slow_request method=%s path=%s status=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
    return response


@app.exception_handler(LLMServiceError)
async def _llm_service_error_handler(request: Request, exc: LLMServiceError) -> JSONResponse:
    """Translate typed LLM provider failures into a user-safe JSON
    response. Sentry breadcrumb keeps the upstream status + provider
    reason searchable without exposing them to end users.

    See ``app/services/llm/errors.py`` for the categorization. All
    cases default to HTTP 503 with a friendly ``detail`` string the
    FE can render via ``extractApiError`` verbatim — no vendor
    messages (e.g. OpenRouter's ``"Insufficient credits..."``) ever
    reach the user.
    """
    _log.warning(
        "llm_service_error path=%s reason=%s upstream_status=%s",
        request.url.path,
        exc.reason,
        exc.upstream_status,
    )
    # ``capture_exception`` is a no-op when Sentry isn't initialized,
    # so the import is cheap and safe in tests.
    try:
        import sentry_sdk

        sentry_sdk.set_tag("llm.reason", exc.reason)
        if exc.upstream_status is not None:
            sentry_sdk.set_tag("llm.upstream_status", str(exc.upstream_status))
        sentry_sdk.capture_exception(exc)
    except ImportError:  # pragma: no cover
        pass
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": exc.user_message, "code": exc.reason},
    )


def _is_upstream_block(exc: APIError) -> bool:
    """True when the response never reached PostgREST at all.

    A real PostgREST error always carries a SQLSTATE-ish ``code`` parsed out of
    a JSON body. This pair — HTTP ``403`` *plus* "JSON could not be generated" —
    is the client telling us the body wasn't JSON, which for this stack means an
    edge/WAF block page rather than the database. Both halves are required: a
    genuine RLS denial is also 403, but it arrives as JSON with code ``42501``
    and must keep its existing handling.
    """
    return str(exc.code) == "403" and exc.message == "JSON could not be generated"


def _cf_ray_id(exc: APIError) -> str | None:
    """The Cloudflare Ray ID from a block page, for correlating in their logs.

    Deliberately narrow: ``exc.details`` holds the whole HTML page, which also
    contains the CALLER'S IP ADDRESS. Never log or return the page itself.
    """
    match = re.search(r"Ray ID:[^<]*<strong[^>]*>([0-9a-f]+)", str(exc.details or ""))
    return match.group(1) if match else None


@app.exception_handler(APIError)
async def _postgrest_error_handler(request: Request, exc: APIError) -> JSONResponse:
    """Map a PostgREST ``22P02`` (invalid text representation) to a clean 404.

    A malformed path id — ``GET /jobs/not-a-uuid`` — flows into
    ``.eq("id", <value>)``, and PostgREST rejects the cast with
    ``22P02 invalid input syntax for type uuid``. Uncaught, that ``APIError``
    falls through to the generic 500 handler below: a *server* error for what
    is really client-supplied garbage — 500 log noise, and the wrong contract
    (the resource simply isn't there).

    **404** (not 400 or 500) is deliberate: it makes a malformed id behave
    exactly like a well-formed-but-absent one, it doesn't leak whether an id
    was merely malformed vs. genuinely missing, and it's the only status the
    web UI renders as a calm "not found" page rather than a red error toast
    (the job detail view special-cases 404). A ``22P02`` reaching here is
    overwhelmingly a malformed-UUID path id — other malformed inputs (ints,
    enums) are typed by Pydantic and rejected as 422 well before Postgres.

    Any *other* PostgREST error is re-raised so the generic handler logs the
    traceback and returns 500 unchanged. The code is logged here (never the raw
    PostgREST message, which can echo the offending input) so a genuinely
    *internal* ``22P02`` — one the app caused, not the client — stays visible
    in the logs instead of silently becoming a 404.

    A route that already catches ``APIError`` locally (e.g. the manual-job
    persist path → 502) still wins: a local ``except`` handles the exception
    before it can ever propagate to this app-wide net.
    """
    if exc.code == "22P02":
        _log.info(
            "postgrest 22P02 (invalid text representation) -> 404 on %s %s",
            request.method,
            request.url.path,
        )
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    if exc.code == "57014":
        # Statement timeout: the database refused the work under load, the
        # request itself was fine. An unhandled 500 here used to dump a full
        # ExceptionGroup traceback per occurrence (two during the 2026-08-05
        # verification drive, #604) and read as a server bug to every retry
        # layer. 503 + Retry-After is the honest contract — the BFF/RSC
        # fetch helpers already retry 5xx once, and both masked exactly this
        # failure in prod.
        _log.warning(
            "postgrest 57014 (statement timeout) -> 503 on %s %s",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "Temporarily overloaded — please retry."},
            headers={"Retry-After": "5"},
        )
    if _is_upstream_block(exc):
        # Supabase sits behind Cloudflare, whose WAF rejects request values that
        # look like an attack — a user typing ``' OR 1=1 --`` into the company
        # or title filter is enough. The block page is HTML, so the client
        # raises "JSON could not be generated" and the request became a 500:
        # a *server* error for a value the perimeter deliberately refused, and
        # a red toast where the UI could have said "try different text".
        #
        # 400, not 5xx: nothing is wrong with the service, and the caller can
        # fix it by rephrasing. Retrying is useless, so this must not look
        # retryable. NOTE the block page embeds the caller's IP and a Ray ID —
        # only the Ray ID is logged, and the body says nothing at all.
        _log.warning(
            "upstream WAF blocked a request value -> 400 on %s %s (cf-ray=%s)",
            request.method,
            request.url.path,
            _cf_ray_id(exc) or "unknown",
        )
        return JSONResponse(
            status_code=400,
            content={"detail": "That filter value was rejected. Please rephrase it."},
        )
    # Not a malformed-input error — hand off to the generic 500 handler, which
    # logs the traceback and applies the fail-closed body posture.
    raise exc


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log full traceback and return a JSON 500.

    Without this, Starlette's default handler returns plain-text
    ``Internal Server Error``, which trips the proxy's non-JSON branch.

    The body is generic by DEFAULT (fail-closed). Verbose detail (class
    name + message) is echoed to the client ONLY when ``DEBUG_ERRORS`` is
    explicitly opted into — the full traceback always goes to the server
    log regardless. The previous gate keyed off
    ``sentry_environment == "production"``, which defaults to "development"
    and is unset in deploy config, so prod was fail-OPEN and leaked
    exception detail (SQL fragments, PostgREST/file paths, secrets in
    stringified exceptions) to any caller who triggered a 500 (audit #29
    round 3 / H5).
    """
    # FastAPI/Starlette resolves more-specific handlers first, so HTTPException
    # never reaches us. Re-raise defensively in case a future middleware path
    # routes one through Exception.
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        raise exc

    _log.exception("unhandled exception on %s %s", request.method, request.url.path)
    body: dict[str, str] = {
        "detail": (
            f"{type(exc).__name__}: {exc}" if settings.debug_errors else "Internal server error"
        ),
        "path": request.url.path,
    }
    return JSONResponse(status_code=500, content=body)


app.include_router(admin.router)
app.include_router(analysis.router)
# All /billing routes 404 outside saas mode / without a Stripe key
# (require_billing) — mounted unconditionally so tests can flip settings.
app.include_router(billing.router)
app.include_router(discovery.router)
app.include_router(experience.router)
app.include_router(feedback.router)
app.include_router(insights.router)
app.include_router(job_search.router)
app.include_router(jobs.router)
app.include_router(keys.router)
app.include_router(poll.router)
app.include_router(public_search.router)
app.include_router(search_events.router)
app.include_router(sources.router)
app.include_router(status.router)
app.include_router(tailor.router)
app.include_router(target_membership.router)
app.include_router(targets.router)
app.include_router(user_profile.router)
app.include_router(waitlist.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Pure: touches no dependency, never 503s while the
    process is up. This is what the Railway healthcheck and the Docker
    HEALTHCHECK target, deliberately — see ``/ready`` below for why."""
    return {"status": "ok"}


@app.get("/version")
async def version() -> dict[str, str | None]:
    """Which build is actually serving traffic.

    Exists because a release could not be verified. The usual discriminator is
    a schema diff in ``/openapi.json``, but a BEHAVIOURAL release changes no
    schema — and with Railway auth expired and no deploy id in any response
    header, there was no way to tell the new build from the old one. Release
    #794 shipped with its API cutover unverified for exactly this reason.

    Unauthenticated and dependency-free, like ``/health``: a probe that needs a
    token is useless in the moment you actually reach for it. It exposes only a
    commit SHA and a build timestamp — no config, no secrets.

    ``RAILWAY_GIT_COMMIT_SHA`` is injected by Railway; ``BUILD_SHA`` is the
    portable override for any other host (and for a local Docker run). Both
    absent → ``null``, which is itself the honest answer rather than a lie.
    """
    return {
        "commit": os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("BUILD_SHA"),
        "built_at": os.getenv("BUILD_TIME"),
        "environment": os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("APP_ENV"),
    }


# How long the readiness DB ping may take before we call the dependency
# unhealthy. Kept short: a slow Supabase is a failing Supabase for the
# purpose of "should the LB send this instance traffic?".
_READY_PING_TIMEOUT_S = 3.0


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe — checks the critical dependency (Supabase) cheaply.

    Returns 200 only when the service-role Supabase client is configured
    AND a lightweight ``SELECT ... LIMIT 1`` against the shared ``sources``
    catalog round-trips within ``_READY_PING_TIMEOUT_S``. Otherwise 503.

    Intended for load-balancer readiness gating and external monitoring,
    NOT for the container restart healthcheck. ``/health`` (liveness)
    stays the restart target on purpose:

    A restart loop triggered by a transient Supabase blip would be
    strictly worse than riding it out — the dependency is external, so
    cycling the container doesn't fix it and only adds cold-start latency
    on top of the outage. So we expose readiness for traffic gating /
    alerting but leave liveness as the thing that decides "kill and
    restart". See railway.toml and the Dockerfile HEALTHCHECK.

    Uses the async service-role client: ``asyncio.wait_for`` actually cancels
    an in-flight async httpx request on timeout, so a slow Supabase can't strand
    a threadpool worker for the full 120s postgrest client timeout — which, at LB
    probe cadence, would starve the shared executor every ``to_thread`` handler
    depends on (Perf-F4). An absent async client means the dependency is
    unconfigured → 503 (the sync + async service clients share one configuration
    guard, so if one is absent both are).
    """
    async_supabase = get_async_supabase()
    if async_supabase is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "dependency": "supabase",
                "reason": "unconfigured",
            },
        )
    ping = async_supabase.table("sources").select("id").limit(1).execute()
    try:
        await asyncio.wait_for(ping, timeout=_READY_PING_TIMEOUT_S)
    except Exception as exc:
        # Covers asyncio.TimeoutError (== TimeoutError in 3.11+) from the
        # wait_for deadline and any transport/SDK error from the ping.
        _log.warning("readiness check failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "dependency": "supabase", "reason": "ping_failed"},
        )
    return JSONResponse(status_code=200, content={"status": "ready"})
