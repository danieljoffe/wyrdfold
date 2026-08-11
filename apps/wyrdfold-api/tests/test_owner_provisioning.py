"""Owner provisioning at boot (Phase 2 slice 1).

Pins the perimeter contract from docs/plan-wyrdfold-deployment-modes.md:
self_host + OWNER_EMAIL -> the owner auth user exists after boot, idempotently;
saas / unset email / failures never touch auth or block startup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.services.owner_provisioning import provision_owner


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "allowed_hosts": "*",
        "deployment_mode": "self_host",
        "owner_email": "owner@example.com",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _sb() -> MagicMock:
    """Async service-client mock (#57): GoTrue admin ``create_user`` is awaited,
    so it must be an ``AsyncMock``."""
    sb = MagicMock()
    sb.auth.admin.create_user = AsyncMock()
    return sb


async def test_self_host_with_owner_email_creates_the_user() -> None:
    sb = _sb()
    await provision_owner(sb, _settings())
    sb.auth.admin.create_user.assert_awaited_once_with(
        {"email": "owner@example.com", "email_confirm": True}
    )


async def test_email_is_normalized_before_create() -> None:
    sb = _sb()
    await provision_owner(sb, _settings(owner_email="  Owner@Example.COM "))
    sb.auth.admin.create_user.assert_awaited_once_with(
        {"email": "owner@example.com", "email_confirm": True}
    )


async def test_saas_mode_never_provisions() -> None:
    sb = _sb()
    await provision_owner(sb, _settings(deployment_mode="saas"))
    sb.auth.admin.create_user.assert_not_awaited()


async def test_unset_owner_email_is_a_noop() -> None:
    sb = _sb()
    await provision_owner(sb, _settings(owner_email=""))
    sb.auth.admin.create_user.assert_not_awaited()


async def test_already_registered_is_idempotent_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sb = _sb()
    sb.auth.admin.create_user.side_effect = Exception(
        "A user with this email address has already been registered"
    )
    with caplog.at_level("INFO"):
        await provision_owner(sb, _settings())
    assert any("already exists" in r.message for r in caplog.records)
    assert not any(r.levelname == "WARNING" for r in caplog.records)


async def test_other_failures_warn_and_do_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient admin-API failure must not block boot — warn with the
    remediation, keep starting."""
    sb = _sb()
    sb.auth.admin.create_user.side_effect = Exception("connection refused")
    with caplog.at_level("WARNING"):
        await provision_owner(sb, _settings())  # must not raise
    assert any(
        "could not create owner@example.com" in r.getMessage()
        or "could not create" in r.getMessage()
        for r in caplog.records
    )


def test_invalid_deployment_mode_is_rejected_at_settings() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _settings(deployment_mode="banana")


async def test_whitespace_only_owner_email_is_a_noop() -> None:
    """Regression (Copilot on #196): '   ' passed the old truthiness check,
    normalized to '', and would have called create_user with an empty email."""
    sb = _sb()
    await provision_owner(sb, _settings(owner_email="   "))
    sb.auth.admin.create_user.assert_not_awaited()
