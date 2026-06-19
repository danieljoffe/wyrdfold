"""LLM plumbing module.

The `LLMClient` Protocol has three implementations:

- `MockLLMClient` — deterministic fake for tests and local dev.
- `AnthropicLLMClient` — production, uses the official `anthropic` SDK
  pointed at api.anthropic.com.
- `OpenRouterLLMClient` — same code path as Anthropic but routed
  through openrouter.ai for unified billing + cross-provider fallback
  (see plan-wyrdfold-openrouter-migration.md).

`get_default_client()` picks based on `settings.llm_provider` (env var
`LLM_PROVIDER=mock|anthropic|openrouter`). Mock is the default so
nothing hits a real API unless opted in.

Every consumer (derive, tailor, conversation) tags calls with a `purpose`
string so cost-log rows can be grouped by feature for spend analysis.
"""

import logging
from typing import TYPE_CHECKING

from app.services.llm.anthropic_client import AnthropicLLMClient
from app.services.llm.client import LLMClient
from app.services.llm.errors import MissingUserKeyError
from app.services.llm.mock import MockLLMClient
from app.services.llm.openrouter_client import OpenRouterLLMClient

if TYPE_CHECKING:
    from supabase import Client

logger = logging.getLogger(__name__)

__all__ = [
    "AnthropicLLMClient",
    "LLMClient",
    "MissingUserKeyError",
    "MockLLMClient",
    "OpenRouterLLMClient",
    "get_client",
    "get_default_client",
]


def get_default_client() -> LLMClient:
    """Return the configured LLM client.

    Reads `settings.llm_provider`. `"anthropic"` and `"openrouter"`
    require their respective API keys to be set (`anthropic_api_key`
    / `openrouter_api_key`); anything else falls back to the mock.
    """
    from app.config import settings

    if settings.llm_provider == "anthropic":
        return AnthropicLLMClient(
            api_key=settings.anthropic_api_key or None,
            timeout=settings.anthropic_timeout_seconds,
            max_retries=settings.anthropic_max_retries,
        )
    if settings.llm_provider == "openrouter":
        return OpenRouterLLMClient(
            api_key=settings.openrouter_api_key or None,
            timeout=settings.openrouter_timeout_seconds,
            max_retries=settings.openrouter_max_retries,
        )
    return MockLLMClient()


def get_client(supabase: "Client | None", user_id: str | None) -> LLMClient:
    """Return the LLM client for a request, honoring BYOK (#5 P2).

    Resolution order:

    1. ``mock`` provider → ``MockLLMClient`` (tests / local dev; never
       reads keys, so nothing hits a real API).
    2. logged-in user with a stored OpenRouter key → a client on **their**
       key, so their inference bills their OpenRouter account.
    3. logged-in user, no key, ``BYOK_REQUIRE_USER_KEYS`` set →
       ``MissingUserKeyError`` (hosted refuses to bill the operator's key
       for a stranger).
    4. otherwise → the instance env key (``get_default_client``): the
       single-tenant self-host default, behavior unchanged.

    ``user_id`` is None for api-key / cron / poller / batch callers — they
    always use the instance key (background spend is attributed and gated
    per payer in the poller; the poller's own per-payer threading is #5
    P3, not here). ``supabase`` may be None when the pool isn't configured
    (mock / local), in which case BYOK resolution is skipped. BYOK is
    OpenRouter-only for v1 (#5 decision 1); embeddings stay on the
    instance Voyage key.
    """
    from app.config import settings

    if settings.llm_provider == "mock":
        return MockLLMClient()

    if user_id and supabase is not None:
        user_key = _user_byok_key(supabase, user_id)
        if user_key:
            return OpenRouterLLMClient(
                api_key=user_key,
                timeout=settings.openrouter_timeout_seconds,
                max_retries=settings.openrouter_max_retries,
            )
        if settings.byok_require_user_keys:
            raise MissingUserKeyError("openrouter")

    return get_default_client()


def _user_byok_key(supabase: "Client", user_id: str) -> str | None:
    """Decrypted OpenRouter key for ``user_id``, or None.

    Returns None — never raises — when BYOK isn't configured (no master
    key) or the stored ciphertext can't be decrypted (rotated / wrong
    master key), so a misconfiguration degrades to the require/fallback
    logic in ``get_client`` rather than 500-ing the request. The decrypt
    failure is logged (not swallowed silently) so a bad master key is
    visible in ops.
    """
    from app.services import keys

    if not keys.is_configured():
        return None
    try:
        return keys.get_key(supabase, user_id=user_id, provider="openrouter")
    except keys.BYOKDecryptError:
        logger.warning("byok_decrypt_failed user=%s provider=openrouter", user_id)
        return None
