"""Supabase clients.

Two clients, two trust levels:

- The **service-role** singleton (`get_supabase_pool`) — created once at
  startup, reused across requests, **bypasses RLS**. Used by background
  work, shared-catalog writes, and the api-key/cron path.
- The **per-request user client** (`get_user_client`) — built per request
  bound to the caller's JWT so Postgres RLS enforces per-user access
  (#79). Each call returns a fresh `Client` whose Authorization header
  carries that request's token (no shared mutable auth state — see the
  token-bleed analysis on #79), but they all share one module-level
  httpx connection pool so there's no per-request socket cost.
"""

from __future__ import annotations

import httpx
from postgrest.constants import DEFAULT_POSTGREST_CLIENT_TIMEOUT
from supabase import Client, ClientOptions, create_client

from app.config import settings

_client: Client | None = None

# Shared httpx connection pool for the per-request user clients. httpx
# clients are thread-safe for requests, so one pool serves the whole
# threadpool; only the lightweight per-request Client wrapper + its own
# headers are allocated per call.
_user_httpx: httpx.Client | None = None


def _build_http1_client() -> httpx.Client:
    """httpx transport for supabase clients — HTTP/1.1, never HTTP/2.

    supabase-py's default postgrest transport sets ``http2=True``. Both
    the shared service-role singleton and the shared per-request user pool
    get hit by many *concurrent* requests at once — the service-role
    client most acutely, since the poller fans out a burst of
    ``asyncio.to_thread`` upserts/queries against the single shared client.

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
    )


def init_supabase() -> None:
    global _client
    if settings.supabase_url and settings.supabase_service_role_key:
        # Force HTTP/1.1 on the service-role transport (see
        # _build_http1_client) — the shared singleton must survive the
        # poller's concurrent to_thread write burst.
        options = ClientOptions(httpx_client=_build_http1_client())
        _client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
            options,
        )


def get_supabase_pool() -> Client | None:
    return _client


def _get_user_httpx() -> httpx.Client:
    global _user_httpx
    if _user_httpx is None:
        # HTTP/1.1 here too: this single pool is shared across all
        # concurrent per-request user clients, so it must be
        # concurrency-safe under load (see _build_http1_client).
        _user_httpx = _build_http1_client()
    return _user_httpx


def get_user_client(access_token: str) -> Client:
    """Build a per-request Supabase client bound to ``access_token``.

    The anon key is the base (so a missing/empty token degrades to anon,
    never service-role), and the caller's JWT is set as the Authorization
    bearer on this request's own client — PostgREST then runs every query
    under that user, so RLS policies apply. Reuses the shared httpx pool.
    """
    options = ClientOptions(httpx_client=_get_user_httpx())
    client = create_client(
        settings.supabase_url, settings.supabase_anon_key, options
    )
    # Bind the bearer on this per-request client only. Safe vs. the
    # service-role singleton's bleed risk because nothing else holds a
    # reference to this client (each call builds a fresh one).
    #
    # postgrest.auth() covers DB queries. Storage (and any other sub-client)
    # is created lazily from `client.options.headers`, so we also set the
    # Authorization there — otherwise storage would keep the anon key and
    # RLS-protected buckets would deny the user their own objects. apikey
    # stays the anon key.
    client.options.headers["Authorization"] = f"Bearer {access_token}"
    client.postgrest.auth(access_token)
    return client


def close_supabase() -> None:
    global _client, _user_httpx
    _client = None
    if _user_httpx is not None:
        _user_httpx.close()
        _user_httpx = None
