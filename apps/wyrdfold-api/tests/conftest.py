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

# ---- #515: local test env == CI clean room ---------------------------------
# WYRDFOLD_API_TESTING=1 (above) suppresses pydantic's env_file source (#28),
# but real environment VARIABLES outrank the file — and nx loads the
# workspace/app `.env` values into the process env before running the test
# target. Ten tests failed locally-but-not-in-CI through that hole (SaaS mode
# switched on, prescan flags flipped, BYOK require-mode… one billing test
# reached the REAL Stripe API from a laptop). Scrub every env var that maps
# to a Settings field — except the ones this conftest deliberately sets —
# then rebuild the singleton IN PLACE so every `from app.config import
# settings` importer sees pure defaults, exactly like CI.
_CONFTEST_OWNED_ENV = {
    "SUPABASE_URL",
    "WYRDFOLD_API_KEY",
    "ALLOWED_HOSTS",
    "RATE_LIMIT_ENABLED",
    "ACTIVITY_TRACKING_ENABLED",
    "WYRDFOLD_API_TESTING",
}


def _scrub_settings_env() -> None:
    from app.config import Settings, settings

    for field_name in Settings.model_fields:
        env_name = field_name.upper()
        if env_name not in _CONFTEST_OWNED_ENV:
            os.environ.pop(env_name, None)
    clean = Settings()
    for field_name in Settings.model_fields:
        object.__setattr__(settings, field_name, getattr(clean, field_name))


_scrub_settings_env()


@pytest.fixture(autouse=True)
def _clear_caches():
    """Prevent cross-test cache pollution from in-memory state."""
    from app.services.analysis import run_registry
    from app.services.poller import _PHASE1_REJECTIONS
    from app.services.tailor import run_registry as tailor_run_registry

    job_list_cache.invalidate()
    # The in-flight analysis (#459) and tailor (#656) registries are
    # module-level dicts; a leaked "running" entry would make the next test's
    # kick dedup to 202 without spawning (LLM never called).
    run_registry.clear_all()
    tailor_run_registry.clear_all()
    # Phase-1 negative-verdict cache (#514) is a module-level TTL dict; a
    # rejection leaked from one poller test would silently skip the LLM in
    # the next one.
    _PHASE1_REJECTIONS.clear()
    yield
    job_list_cache.invalidate()
    run_registry.clear_all()
    tailor_run_registry.clear_all()
    _PHASE1_REJECTIONS.clear()


@pytest.fixture(autouse=True)
def _reset_http_client():
    """Reset module-level cached httpx-backed clients between tests.

    `app.http_client.get_http_client()` lazily caches an `AsyncClient`
    bound to whichever event loop first triggered creation. With
    pytest-asyncio's default function-scope loop, that client outlives
    its loop — the next async test reuses the cached handle and fails
    with `RuntimeError: Event loop is closed`. Clearing the reference
    on entry makes each test create a fresh client on its own loop.
    See #28.

    Also resets the SSRF-safe client (#192) and the pooled LLM/embeddings
    provider clients (#192 P-H3): those wrap the same loop-bound
    `AsyncClient`, so a client cached in one test's loop would blow up the
    next test with `Event loop is closed`.
    """
    import app.http_client as http_mod
    from app.services.embeddings import reset_embeddings_client_cache
    from app.services.llm import reset_llm_client_cache

    def _reset() -> None:
        http_mod._client = None
        http_mod._safe_client = None
        reset_llm_client_cache()
        reset_embeddings_client_cache()

    _reset()
    yield
    _reset()


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


@pytest.fixture(autouse=True)
def _reset_spend_memo():
    """#642: the day-spend TTL memo is module-global state — reset per test
    so each test's mocked meter value is actually read (a stale cached value
    otherwise leaks across tests and breaks budget-gate assertions)."""
    from app.services import poller as _poller_mod

    _poller_mod._spend_memo.update(at=0.0, midnight=None, value=0.0)
    yield
    _poller_mod._spend_memo.update(at=0.0, midnight=None, value=0.0)
