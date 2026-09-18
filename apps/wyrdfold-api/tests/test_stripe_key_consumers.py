"""The Stripe key both runtime seams consume is the one the startup guard checked (#1079).

In plain terms: the key is cleaned of stray spaces once, when settings load.
These tests prove that the two places that actually hand a key to Stripe
(the billing router's client and the reconciliation sweep's default client)
receive that cleaned value, so a later change that read the raw value would
turn one of these red while the settings-level test stayed green.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import Settings

PADDED = "  sk_live_abc123 \n"
CLEAN = "sk_live_abc123"


def _saas_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        deployment_mode="saas",
        stripe_secret_key=PADDED,
        allowed_hosts="*",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="sk-test",
        supabase_anon_key="anon-test",
        llm_provider="mock",
        embeddings_provider="mock",
    )


def test_billing_router_client_receives_the_normalized_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import billing

    constructor = MagicMock(name="StripeClient")
    monkeypatch.setattr(billing, "settings", _saas_settings())
    monkeypatch.setattr(billing.stripe, "StripeClient", constructor)

    billing._client()

    constructor.assert_called_once_with(CLEAN)


@pytest.mark.asyncio
async def test_reconcile_default_client_receives_the_normalized_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stripe

    from app.services import billing_reconcile

    constructor = MagicMock(name="StripeClient")
    monkeypatch.setattr(billing_reconcile, "settings", _saas_settings())
    monkeypatch.setattr(stripe, "StripeClient", constructor)

    report: dict[str, Any] = await billing_reconcile.reconcile_billing(MagicMock(), client=None)

    constructor.assert_called_once_with(CLEAN)
    # The sweep is best-effort: a client that cannot list subscriptions is
    # reported as skipped, never raised. Only the construction matters here.
    assert report["checked"] == 0
