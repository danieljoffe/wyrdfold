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
from supabase import Client, ClientOptions, create_client

from app.config import settings

_client: Client | None = None

# Shared httpx connection pool for the per-request user clients. httpx
# clients are thread-safe for requests, so one pool serves the whole
# threadpool; only the lightweight per-request Client wrapper + its own
# headers are allocated per call.
_user_httpx: httpx.Client | None = None


def init_supabase() -> None:
    global _client
    if settings.supabase_url and settings.supabase_service_role_key:
        _client = create_client(settings.supabase_url, settings.supabase_service_role_key)


def get_supabase_pool() -> Client | None:
    return _client


def _get_user_httpx() -> httpx.Client:
    global _user_httpx
    if _user_httpx is None:
        _user_httpx = httpx.Client()
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
    # Set the bearer on this per-request client only. Safe vs. the
    # service-role singleton's bleed risk because nothing else holds a
    # reference to this client.
    client.postgrest.auth(access_token)
    return client


def close_supabase() -> None:
    global _client, _user_httpx
    _client = None
    if _user_httpx is not None:
        _user_httpx.close()
        _user_httpx = None
