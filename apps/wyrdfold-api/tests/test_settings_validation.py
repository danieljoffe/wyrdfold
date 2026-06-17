"""Boot-time settings validation in ``app.main._validate_settings`` (#30 F2).

Pins the "fail fast" contract so a future Settings refactor can't
silently drop a check and turn a misconfig into a runtime 503.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import _validate_settings


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
        _validate_settings(
            _good_settings(llm_provider="anthropic", anthropic_api_key="")
        )


def test_voyage_provider_without_key_fails_boot() -> None:
    with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
        _validate_settings(
            _good_settings(embeddings_provider="voyage", voyage_api_key="")
        )
