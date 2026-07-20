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
