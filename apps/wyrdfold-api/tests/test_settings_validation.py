"""Boot-time settings validation in ``app.main._validate_settings`` (#30 F2).

Pins the "fail fast" contract so a future Settings refactor can't
silently drop a check and turn a misconfig into a runtime 503.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.main import _probe_supabase_keys, _validate_settings


def _good_settings(**overrides: object) -> Settings:
    """Build a Settings that passes every gate by default.

    Tests override individual fields to assert each gate independently.
    """
    base: dict[str, object] = {
        "allowed_hosts": "*",
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "sk-test",
        "supabase_anon_key": "anon-test",
        "llm_provider": "mock",
        "embeddings_provider": "mock",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_valid_settings_pass() -> None:
    _validate_settings(_good_settings())


def test_missing_allowed_hosts_fails_boot() -> None:
    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS"):
        _validate_settings(_good_settings(allowed_hosts=""))


def test_missing_supabase_url_fails_boot() -> None:
    """The whole point of #30 F2 — a self-hoster forgetting to set
    SUPABASE_URL gets a clear startup error, not a silent 503 on the
    first authenticated request."""
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        _validate_settings(_good_settings(supabase_url=""))


def test_missing_supabase_service_role_key_fails_boot() -> None:
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        _validate_settings(_good_settings(supabase_service_role_key=""))


def test_missing_supabase_anon_key_fails_boot() -> None:
    """A deploy with the service-role key but no anon key boots clean, then
    503s every per-user RLS route (#79). Caught prod this exact way — fail
    loudly at startup instead."""
    with pytest.raises(RuntimeError, match="SUPABASE_ANON_KEY"):
        _validate_settings(_good_settings(supabase_anon_key=""))


def test_anthropic_provider_without_key_fails_boot() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _validate_settings(_good_settings(llm_provider="anthropic", anthropic_api_key=""))


def test_voyage_provider_without_key_fails_boot() -> None:
    with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
        _validate_settings(_good_settings(embeddings_provider="voyage", voyage_api_key=""))


# ---- Supabase key liveness probe (_probe_supabase_keys) --------------------
# A key can be *set* (passes _validate_settings) yet *disabled* — the gateway
# rejects it and every request 500s. The 2026-07-02 incident: the /jobs path
# flipped onto the RLS user client whose prod anon key was a disabled legacy
# key. The probe turns that into a boot failure.

_LEGACY_DISABLED_BODY = (
    '{"message":"Legacy API keys are disabled",'
    '"hint":"Your legacy API keys were disabled on 2026-06-23."}'
)


async def test_probe_fails_boot_on_disabled_legacy_key() -> None:
    async def _fetch(_url: str, _key: str) -> str:
        return _LEGACY_DISABLED_BODY

    with pytest.raises(RuntimeError, match="DISABLED legacy"):
        await _probe_supabase_keys(_good_settings(), fetch=_fetch)


async def test_probe_passes_on_gateway_accepted_key() -> None:
    async def _fetch(_url: str, _key: str) -> str:
        # What a valid (publishable) key returns at /rest/v1/ — no signature.
        return '{"message":"Secret API key required"}'

    await _probe_supabase_keys(_good_settings(), fetch=_fetch)  # no raise


async def test_probe_tolerates_unreachable_supabase() -> None:
    """A network blip must NOT block boot — only the deterministic
    disabled-key signature does."""

    async def _fetch(_url: str, _key: str) -> str:
        raise httpx.ConnectError("supabase unreachable")

    await _probe_supabase_keys(_good_settings(), fetch=_fetch)  # no raise


async def test_probe_warns_on_legacy_format_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _fetch(_url: str, _key: str) -> str:
        return "{}"

    with caplog.at_level("WARNING"):
        await _probe_supabase_keys(
            _good_settings(supabase_anon_key="eyJhbGciOiJIUzI1NiJ9.legacy.sig"),
            fetch=_fetch,
        )
    assert any("legacy JWT-format" in r.message for r in caplog.records)


# ---- Stripe key mode vs environment (#1079) ---------------------------------
#
# A test key on production means nobody can pay; a live key anywhere else means
# a test run charges real cards. Both happened once (#861). Every cell below
# is a startup decision, so every cell has a test, and the refusing cells are
# proven to refuse: a guard that passes everything is worse than none.


def _named(monkeypatch: pytest.MonkeyPatch, environment: str | None) -> None:
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    if environment is not None:
        monkeypatch.setenv("APP_ENV", environment)


@pytest.mark.parametrize(
    ("environment", "key"),
    [
        ("production", "sk_live_abc123"),
        ("staging", "sk_test_abc123"),
        ("development", "sk_test_abc123"),
        ("local", "sk_test_abc123"),
        # whitespace and case are canonicalized once, in the shared helper
        ("  Production ", " sk_live_abc123 "),
    ],
)
def test_stripe_key_mode_matching_its_environment_starts(
    monkeypatch: pytest.MonkeyPatch, environment: str, key: str
) -> None:
    _named(monkeypatch, environment)
    _validate_settings(_good_settings(stripe_secret_key=key))


@pytest.mark.parametrize(
    ("environment", "key", "fragment"),
    [
        ("production", "sk_test_abc123", "TEST Stripe key"),
        ("staging", "sk_live_abc123", "LIVE Stripe key"),
        ("development", "sk_live_abc123", "LIVE Stripe key"),
        ("local", "sk_live_abc123", "LIVE Stripe key"),
    ],
)
def test_stripe_key_mode_mismatch_refuses_to_start_without_naming_the_key(
    monkeypatch: pytest.MonkeyPatch, environment: str, key: str, fragment: str
) -> None:
    _named(monkeypatch, environment)
    with pytest.raises(RuntimeError, match=fragment) as exc:
        _validate_settings(_good_settings(stripe_secret_key=key))
    assert "abc123" not in str(exc.value)


@pytest.mark.parametrize("environment", ["production", "local", "not-a-real-env", None])
def test_empty_stripe_key_skips_the_mode_check(
    monkeypatch: pytest.MonkeyPatch, environment: str | None
) -> None:
    """Billing disabled is a valid configuration everywhere, named or not."""
    _named(monkeypatch, environment)
    _validate_settings(_good_settings(stripe_secret_key=""))
    _validate_settings(_good_settings(stripe_secret_key="   "))


def test_stripe_key_with_an_unnamed_environment_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case that recreates #861: a key with no environment to check it
    against must not start as "not production"."""
    _named(monkeypatch, None)
    with pytest.raises(RuntimeError, match="unnamed"):
        _validate_settings(_good_settings(stripe_secret_key="sk_test_abc123"))
    with pytest.raises(RuntimeError, match="unnamed"):
        _validate_settings(_good_settings(stripe_secret_key="sk_live_abc123"))


@pytest.mark.parametrize("environment", ["prod", "Prod-2", "production-eu", "   "])
def test_stripe_key_with_an_unknown_environment_name_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    _named(monkeypatch, environment)
    with pytest.raises(RuntimeError, match="APP_ENV"):
        _validate_settings(_good_settings(stripe_secret_key="sk_test_abc123"))


@pytest.mark.parametrize("key", ["pk_live_abc123", "rk_test_abc123", "sk-live-abc123", "abc123"])
def test_stripe_key_with_an_unknown_prefix_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    _named(monkeypatch, "production")
    with pytest.raises(RuntimeError, match="neither an sk_test_ nor an sk_live_") as exc:
        _validate_settings(_good_settings(stripe_secret_key=key))
    assert "abc123" not in str(exc.value)


def test_railway_environment_name_wins_over_app_env_like_version_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard and ``GET /version`` read the same helper with the same
    precedence, so a deploy can never report one environment and check
    another."""
    from app.config import runtime_environment

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", " Production ")
    monkeypatch.setenv("APP_ENV", "local")
    assert runtime_environment() == "production"
    with pytest.raises(RuntimeError, match="TEST Stripe key"):
        _validate_settings(_good_settings(stripe_secret_key="sk_test_abc123"))
