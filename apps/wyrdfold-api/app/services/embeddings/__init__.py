"""Embeddings module.

The `EmbeddingsClient` Protocol has two implementations:

- `MockEmbeddingsClient` — deterministic fake for tests and local dev.
- `VoyageEmbeddingsClient` — production, uses the official `voyageai` SDK.

`get_default_client()` picks based on `settings.embeddings_provider` (env
var `EMBEDDINGS_PROVIDER=mock|voyage`). Mock is the default so nothing
hits the real API unless opted in.
"""

from functools import lru_cache

from app.services.embeddings.client import EmbeddingsClient
from app.services.embeddings.mock import MockEmbeddingsClient
from app.services.embeddings.voyage_client import VoyageEmbeddingsClient

__all__ = [
    "EmbeddingsClient",
    "MockEmbeddingsClient",
    "VoyageEmbeddingsClient",
    "get_default_client",
    "reset_embeddings_client_cache",
]

# The Voyage client wraps an httpx connection pool; reuse it across requests
# rather than rebuilding a pool per call (#192 P-H3). Bounded LRU keyed by
# ``(api_key, timeout, max_retries)``; the mock is never cached.
_CLIENT_CACHE_SIZE = 8


@lru_cache(maxsize=_CLIENT_CACHE_SIZE)
def _cached_voyage(api_key: str | None, timeout: float, max_retries: int) -> VoyageEmbeddingsClient:
    return VoyageEmbeddingsClient(api_key=api_key, timeout=timeout, max_retries=max_retries)


def reset_embeddings_client_cache() -> None:
    """Clear the cached Voyage client (test isolation / config changes)."""
    _cached_voyage.cache_clear()


def get_default_client() -> EmbeddingsClient:
    """Return the configured embeddings client.

    Reads `settings.embeddings_provider`. `"voyage"` requires
    `VOYAGE_API_KEY` (via env or `voyage_api_key` setting); anything
    else falls back to the mock. The Voyage client is pooled/reused across
    requests (see ``_cached_voyage`` — #192 P-H3); the mock is always fresh.
    """
    from app.config import settings

    if settings.embeddings_provider == "voyage":
        return _cached_voyage(
            settings.voyage_api_key or None,
            settings.voyage_timeout_seconds,
            settings.voyage_max_retries,
        )
    return MockEmbeddingsClient()
