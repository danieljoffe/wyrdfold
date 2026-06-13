import os

# Tell Settings to skip the dev .env file (see app/config.py). Without this,
# experimental flags in the developer's local .env (RECENCY_DECAY_ENABLED,
# PHASE1_TRIAGE_ENABLED, etc.) switch unmocked code paths during pytest and
# cause spurious failures that don't reproduce in CI. Issue #28.
os.environ["WYRDFOLD_API_TESTING"] = "1"

# Set required env vars BEFORE importing the app so Settings picks them up.
os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
os.environ.setdefault("WYRDFOLD_API_KEY", "testkey")
# Force-overwrite so a local .env with restrictive hosts can't break tests.
os.environ["ALLOWED_HOSTS"] = "*"
# Disable HTTP rate limiting in tests — many tests hammer the same endpoint
# from a single TestClient (one IP, no JWT), which would trip the limiter
# and turn legitimate test runs into flaky 429s.
os.environ["RATE_LIMIT_ENABLED"] = "false"
# Disable last_seen activity stamping — it fires inside the auth deps and
# would attempt real Supabase writes from tests that only mock per-route
# clients.
os.environ["ACTIVITY_TRACKING_ENABLED"] = "false"

from unittest.mock import MagicMock

import pytest

from app.cache import job_list_cache


@pytest.fixture(autouse=True)
def _clear_caches():
    """Prevent cross-test cache pollution from the in-memory TTL cache."""
    job_list_cache.invalidate()
    yield
    job_list_cache.invalidate()


@pytest.fixture(autouse=True)
def _reset_http_client():
    """Reset the module-level cached httpx client between tests.

    `app.http_client.get_http_client()` lazily caches an `AsyncClient`
    bound to whichever event loop first triggered creation. With
    pytest-asyncio's default function-scope loop, that client outlives
    its loop — the next async test reuses the cached handle and fails
    with `RuntimeError: Event loop is closed`. Clearing the reference
    on entry makes each test create a fresh client on its own loop.
    See #28.
    """
    import app.http_client as http_mod

    http_mod._client = None
    yield
    http_mod._client = None


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Skip retry backoff in tests.

    The shared HTTP helper retries on 429/5xx/transport errors with
    exponential backoff. In production that's seconds of sleep per
    failure; in tests that's seconds of pointless wall-clock time.
    Patch the module-level sleep alias to an immediate no-op.
    """

    async def _instant(_seconds: float) -> None:
        return None

    import app.http_client as http_mod

    monkeypatch.setattr(http_mod, "_sleep", _instant)
    yield


@pytest.fixture(autouse=True)
def _bypass_ssrf_dns(monkeypatch):
    """Make every hostname resolve to a public address for unit tests.

    Real DNS lookups are noisy and slow, and the mock domains used by
    URL-fetcher tests (`legit.com`, `example.com`) sometimes don't
    resolve in sandboxed CI. The SSRF guard added in
    `app/services/validate.py` would reject those as `did not resolve`
    even when the test mocks the HTTP layer. Override the resolver to
    return a public IP so the SSRF check passes — tests that
    specifically need to exercise SSRF rejection re-monkeypatch inside
    the test body.
    """
    import ipaddress

    import app.services.validate as validate_mod

    def _stub_resolve(_hostname: str):
        return [ipaddress.ip_address("1.1.1.1")]

    monkeypatch.setattr(validate_mod, "_resolve_addresses", _stub_resolve)
    yield


@pytest.fixture
def mock_http_client():
    """Provides a mock httpx client injected into the http_client module.

    Usage in tests:
        async def test_something(mock_http_client):
            mock_http_client.get = AsyncMock(return_value=mock_response)
            result = await some_fetcher("token")
    """
    import app.http_client as http_mod

    client = MagicMock()
    client.is_closed = False
    original = http_mod._client
    http_mod._client = client
    yield client
    http_mod._client = original
