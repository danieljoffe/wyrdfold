import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Receive, Scope, Send

from app.config import Settings, settings
from app.http_client import close_http_client
from app.logging_config import init_logging
from app.observability import init_sentry
from app.rate_limit import limiter
from app.routers import (
    admin,
    analysis,
    discovery,
    experience,
    feedback,
    insights,
    jobs,
    keys,
    poll,
    sources,
    status,
    tailor,
    targets,
    user_profile,
)
from app.scheduler import start_scheduler_if_enabled
from app.services.llm.cost_log_buffer import buffer as cost_log_buffer
from app.services.llm.errors import LLMServiceError
from app.supabase_pool import close_supabase, get_supabase_pool, init_supabase

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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _validate_settings(settings)
    init_supabase()
    scheduler = start_scheduler_if_enabled()
    # Background cost-log flush task. Cron paths enqueue rows and the
    # buffer drains them in a single bulk INSERT every few seconds.
    # Started only when supabase is configured (otherwise enqueued rows
    # would accumulate forever in tests/local dev without a backing DB).
    supabase_for_buffer = get_supabase_pool()
    if supabase_for_buffer is not None:
        cost_log_buffer.start(supabase_for_buffer)
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if supabase_for_buffer is not None:
            await cost_log_buffer.stop(supabase_for_buffer)
        close_supabase()
        await close_http_client()


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
async def _rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
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
async def _llm_service_error_handler(
    request: Request, exc: LLMServiceError
) -> JSONResponse:
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
            f"{type(exc).__name__}: {exc}"
            if settings.debug_errors
            else "Internal server error"
        ),
        "path": request.url.path,
    }
    return JSONResponse(status_code=500, content=body)


app.include_router(admin.router)
app.include_router(analysis.router)
app.include_router(discovery.router)
app.include_router(experience.router)
app.include_router(feedback.router)
app.include_router(insights.router)
app.include_router(jobs.router)
app.include_router(keys.router)
app.include_router(poll.router)
app.include_router(sources.router)
app.include_router(status.router)
app.include_router(tailor.router)
app.include_router(targets.router)
app.include_router(user_profile.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Pure: touches no dependency, never 503s while the
    process is up. This is what the Railway healthcheck and the Docker
    HEALTHCHECK target, deliberately — see ``/ready`` below for why."""
    return {"status": "ok"}


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

    Wrapped in ``asyncio.to_thread`` because supabase-py is synchronous;
    a bare ``.execute()`` here would block the event loop (repo #107
    convention, enforced by
    ``tests/test_no_blocking_supabase_in_async_handlers.py``).
    """
    supabase = get_supabase_pool()
    if supabase is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "dependency": "supabase", "reason": "unconfigured"},
        )
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                lambda: supabase.table("sources").select("id").limit(1).execute()
            ),
            timeout=_READY_PING_TIMEOUT_S,
        )
    except Exception as exc:
        # Covers asyncio.TimeoutError (== TimeoutError in 3.11+) from the
        # wait_for deadline and any transport/SDK error from the ping.
        _log.warning("readiness check failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "dependency": "supabase", "reason": "ping_failed"},
        )
    return JSONResponse(status_code=200, content={"status": "ready"})
