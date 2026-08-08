"""Supabase clients.

Two trust levels:

- The **service-role** singleton (`get_async_supabase`) — created once at
  startup, reused across requests, **bypasses RLS**. Used by background work,
  shared-catalog writes, the api-key/cron path, and every interactive dependency
  since #57 PR-G2e-8 retired the sync service singleton. Standalone scripts that
  run outside the app lifespan (no event loop, no singleton) build a one-off sync
  service client via `create_service_client`.
- The **per-request user client** (`get_async_user_client`) — built per request
  bound to the caller's JWT so Postgres RLS enforces per-user access (#79). Each
  call returns a fresh `AsyncClient` whose Authorization header carries that
  request's token (no shared mutable auth state — see the token-bleed analysis
  on #79), but they all share one module-level async httpx connection pool so
  there's no per-request socket cost. The sync per-request client was retired in
  #57 PR-F once every RLS route moved onto this async client.
"""

from __future__ import annotations

import logging

import httpx
from postgrest.constants import DEFAULT_POSTGREST_CLIENT_TIMEOUT
from supabase import AsyncClient, Client, ClientOptions, acreate_client, create_client
from supabase.lib.client_options import AsyncClientOptions

from app.config import settings

logger = logging.getLogger(__name__)

# Async service-role client (#57). Created in the app lifespan (``acreate_client``
# is async) and reused across requests within the single event loop. Since
# PR-G2e-8 this is the ONLY long-lived service-role client — the sync ``_client``
# singleton it was stood up alongside is gone, and the last interactive deps
# (auth / LLM-budget / LLM-client) now run on this one.
_async_client: AsyncClient | None = None
_async_httpx: httpx.AsyncClient | None = None

# Shared httpx pool for the per-request ASYNC user clients (#57). One bounded
# HTTP/2 pool (safe multiplexed in a single event loop, unlike a sync pool which
# must pin HTTP/1.1 for thread-safety) shared across all concurrent per-request
# user clients, so only the lightweight per-request ``AsyncClient`` wrapper + its
# own bearer are allocated per call. The shared pool carries NO Authorization —
# each request's token lives on its own client only, so there is no token bleed
# (see the #79 analysis behind ``get_async_user_client``).
_async_user_httpx: httpx.AsyncClient | None = None

# Deliberate ceiling for the sync service-role postgrest pool (the sync
# per-request user pool was retired in #57 PR-F). The poller's to_thread fan-out
# is already gated well below this by its own semaphores; this just caps the
# accidental worst case.
_SYNC_MAX_CONNECTIONS = 25
_SYNC_MAX_KEEPALIVE = 10


def _build_http1_client() -> httpx.Client:
    """httpx transport for supabase clients — HTTP/1.1, never HTTP/2.

    supabase-py's default postgrest transport sets ``http2=True``. The
    shared service-role singleton gets hit by many *concurrent* requests at
    once, since the poller fans out a burst of ``asyncio.to_thread``
    upserts/queries against the single shared client.

    httpcore's HTTP/2 connection object is **not** safe for concurrent use
    from multiple threads: under the poll burst its streams interleave and
    corrupt, surfacing in prod as
    ``LocalProtocolError: Received pseudo-header in trailer`` /
    ``KeyError`` inside ``httpcore/_sync/http2.py`` plus a flood of broken
    pipes and ``Server disconnected`` once the pooler drops the socket.

    An HTTP/1.1 connection *pool* multiplexes concurrent requests across
    separate connections, so the burst is safe. We mirror the postgrest-py
    transport defaults (``follow_redirects=True`` + its default timeout);
    auth/apikey headers are applied per-request by the postgrest client
    itself, so they don't need to live on this transport.
    """
    return httpx.Client(
        http2=False,
        follow_redirects=True,
        timeout=DEFAULT_POSTGREST_CLIENT_TIMEOUT,
        # Bound the pool deliberately. Without limits httpx defaults to 100 max
        # connections on the service-role singleton, undercutting the
        # small-instance IO posture the async pool is explicitly capped for. In
        # practice anyio's threadpool bounds it, but that ceiling was accidental;
        # make it intentional (hardening review 2026-07-21, Perf-F6).
        limits=httpx.Limits(
            max_connections=_SYNC_MAX_CONNECTIONS,
            max_keepalive_connections=_SYNC_MAX_KEEPALIVE,
        ),
    )


# Bound the async client's connection pool so the migrated poller fan-out can't
# open more sockets to the Supabase pooler (PgBouncer, transaction mode) than it
# can take. Sized for the poll burst with headroom; the per-op concurrency will
# also be gated by an asyncio.Semaphore when the hot paths migrate (#57). HTTP/2
# multiplexes many requests over few connections, so this is generous.
_ASYNC_MAX_CONNECTIONS = 20
_ASYNC_MAX_KEEPALIVE = 10


class _GoawayRetryTransport(httpx.AsyncBaseTransport):
    """Retry a request once when the HTTP/2 peer closes the connection.

    A long-lived HTTP/2 connection has a finite stream budget (Supabase's edge
    caps it at 20,000). When it runs out the server sends GOAWAY, httpcore
    raises ``ConnectionTerminated``, and httpx surfaces it as
    ``RemoteProtocolError`` — killing whatever request happened to be in flight.
    Prod, 2026-08-08::

        Failed to record Phase 1 cost for target 012202b0-…
        httpcore.RemoteProtocolError: <ConnectionTerminated error_code:0,
                                       last_stream_id:19999>

    That write was swallowed by its caller's ``except``, so the LLM spend was
    never recorded — silent, recurring data loss every ~20k requests on the
    pooled service client, and it can hit ANY Supabase call, not just cost logs.

    Retrying is safe *by protocol*, not by optimism: GOAWAY's ``last_stream_id``
    is the highest stream the peer actually processed, and a request that raises
    this error was assigned a stream ABOVE it — so the server provably never
    saw it. That makes the retry correct even for non-idempotent writes like the
    INSERT above, which a blanket retry policy could not claim. We retry once;
    the pool has already discarded the dead connection, so the attempt lands on
    a fresh one. A second failure is a real fault and propagates.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._inner.handle_async_request(request)
        except httpx.RemoteProtocolError as exc:
            # Only GOAWAY-shaped terminations are provably unprocessed. A
            # mid-body protocol error is NOT safe to replay, so let it through.
            if "ConnectionTerminated" not in str(exc):
                raise
            logger.warning(
                "HTTP/2 GOAWAY from Supabase (%s) — retrying %s %s on a fresh "
                "connection; the peer never processed this stream",
                exc,
                request.method,
                request.url.path,
            )
            return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _build_async_http2_client() -> httpx.AsyncClient:
    """httpx.AsyncClient for the async service-role client — HTTP/2 ON.

    The *sync* client is pinned to HTTP/1.1 because httpcore's sync HTTP/2
    connection isn't thread-safe under the poller's ``to_thread`` fan-out (see
    ``_build_http1_client``). The async client has no such problem: it runs
    entirely in one event loop, where httpx's HTTP/2 IS concurrency-safe. So we
    re-enable HTTP/2 here (multiplexing → fewer connections to the pooler, fewer
    handshakes) — the payoff #57 is after. Connection limits keep the pooler
    safe under the migrated poll burst.
    """
    limits = httpx.Limits(
        max_connections=_ASYNC_MAX_CONNECTIONS,
        max_keepalive_connections=_ASYNC_MAX_KEEPALIVE,
    )
    return httpx.AsyncClient(
        # Wrapped so a GOAWAY at the connection's stream ceiling can't take a
        # request down with it — see _GoawayRetryTransport.
        transport=_GoawayRetryTransport(
            httpx.AsyncHTTPTransport(http2=True, limits=limits)
        ),
        follow_redirects=True,
        timeout=DEFAULT_POSTGREST_CLIENT_TIMEOUT,
        limits=limits,
    )


async def init_async_supabase() -> None:
    """Create the async service-role client (#57). Await in the app lifespan.

    No-op when Supabase isn't configured, so local/test runs without a
    service-role key simply have no async client.
    """
    global _async_client, _async_httpx
    if settings.supabase_url and settings.supabase_service_role_key:
        _async_httpx = _build_async_http2_client()
        options = AsyncClientOptions(httpx_client=_async_httpx)
        _async_client = await acreate_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
            options,
        )


def get_async_supabase() -> AsyncClient | None:
    """The async service-role client, or None when unconfigured/not yet inited."""
    return _async_client


async def close_async_supabase() -> None:
    global _async_client, _async_httpx
    _async_client = None
    if _async_httpx is not None:
        await _async_httpx.aclose()
        _async_httpx = None


def _get_async_user_httpx() -> httpx.AsyncClient:
    """Lazily build the shared async user pool on first use (on the loop)."""
    global _async_user_httpx
    if _async_user_httpx is None:
        # Same bounded HTTP/2 transport as the async service client. Safe to
        # multiplex here because every per-request user client runs in the one
        # event loop. Pool size is tunable at the #57 load-test gate.
        _async_user_httpx = _build_async_http2_client()
    return _async_user_httpx


async def get_async_user_client(access_token: str) -> AsyncClient:
    """Async per-request Supabase client bound to ``access_token`` (#57).

    The anon key is the base (so a missing token degrades to anon, never
    service-role) and the caller's JWT is set as the bearer on THIS request's own
    client, so PostgREST runs every query under that user and RLS applies. Reuses
    the shared async httpx pool. This is the per-request user client for every
    RLS route since PR-F retired the sync per-request client.

    The bearer is passed in ``AsyncClientOptions.headers`` up front: it binds
    Authorization for the lazily-created storage sub-client AND makes
    ``AsyncClient.create`` skip its ``get_session()`` network round-trip (it only
    fetches a session when no Authorization header is present), so the
    per-request build is pure construction — no I/O. ``postgrest.auth`` binds the
    DB path. Nothing else references this client, so there is no shared-auth
    bleed (each call builds a fresh client).
    """
    options = AsyncClientOptions(
        httpx_client=_get_async_user_httpx(),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    client = await acreate_client(settings.supabase_url, settings.supabase_anon_key, options)
    # Bind the bearer on the postgrest sub-client for DB queries (the options
    # header above covers storage).
    client.postgrest.auth(access_token)
    return client


async def close_async_user_client() -> None:
    global _async_user_httpx
    if _async_user_httpx is not None:
        await _async_user_httpx.aclose()
        _async_user_httpx = None


def create_service_client() -> Client:
    """Build a one-off sync service-role ``Client`` for standalone scripts.

    Backfills / diagnostics / seeders under ``scripts/`` run as short-lived
    processes OUTSIDE the app lifespan — no running event loop, no lifespan to
    create the pooled async singleton — so they can't use ``get_async_supabase``.
    This returns a freshly-built sync client on demand (NOT a reused singleton),
    which is what lets the API request path carry no sync service client at all:
    #57 PR-G2e-8 deleted the sync service-role singleton (and its lifespan
    init/close) once the last three sync deps moved onto the async client.

    HTTP/1.1-pinned via ``_build_http1_client`` (see it for why) so a script that
    fans work out across threads stays safe. Raises when the service-role
    credentials aren't configured — a script has no meaningful fallback.
    """
    if not (settings.supabase_url and settings.supabase_service_role_key):
        raise RuntimeError("Supabase service-role client not configured")
    options = ClientOptions(httpx_client=_build_http1_client())
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        options,
    )
