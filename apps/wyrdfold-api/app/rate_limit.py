"""HTTP rate limiting via slowapi.

Per-user (JWT-keyed) limits on expensive or fan-out endpoints — LLM-backed
generation, Brave-fan-out source discovery, URL validation, and user-driven
job creation. Closes #850 S2 ("no HTTP rate limiting"), which left the API's
expensive non-LLM endpoints with zero protection.

In-memory backend is correct for the current single-replica Railway deploy.
When scaling to multiple replicas, swap to a Redis storage URI via
``Limiter(storage_uri="redis://…")`` — otherwise each replica enforces its
own bucket and the combined limit is N× the configured ceiling.

Key strategy:
  - JWT callers are keyed by ``jwt:<sub>`` so limits track real users
    regardless of IP (NAT, mobile network changes).
  - Unauthenticated / api-key fallback is keyed by client IP so bad actors
    can't trivially bypass by dropping the Bearer header.
  - Failures decoding the JWT (malformed, expired) fall through to IP keying;
    they'll be rejected by the endpoint's auth dependency anyway, so the
    limiter just protects against pre-auth flooding.
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.dependencies import try_decode_jwt_sub_cache_only


def _user_or_ip_key(request: Request) -> str:
    """Return ``jwt:<sub>`` when a valid Supabase JWT is present, else
    ``ip:<host>``.

    Runs synchronously inside slowapi's middleware — on the event loop, on every
    request. So it decodes against the pre-warmed, out-of-band-refreshed JWKS
    key set (``try_decode_jwt_sub_cache_only``) and NEVER does a blocking network
    fetch: a token whose key id isn't in the warm set falls back to IP keying
    rather than stalling the loop on a JWKS round-trip (Perf-F1). The signature
    is still verified against real keys, so a returned ``sub`` is authentic.
    """
    sub = try_decode_jwt_sub_cache_only(request, settings)
    if sub:
        return f"jwt:{sub}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=_user_or_ip_key,
    enabled=settings.rate_limit_enabled,
    # Default limits — endpoints set their own via ``@limiter.limit(...)``,
    # but this keeps any future undeclared endpoint from being uncovered.
    default_limits=["120/minute"],
)
