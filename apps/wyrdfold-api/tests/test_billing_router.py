"""Stripe billing routes (Phase 3 slice 3).

Pins the billing surface's contract with REAL signature crypto on the
webhook (events are HMAC-signed exactly the way Stripe signs them, so the
verification path — not a mock of it — accepts/rejects):

- the whole surface 404s outside saas mode / without a key;
- checkout creates-or-reuses the Stripe customer and returns the hosted
  URL; unconfigured price → 503;
- portal 409s before a customer exists;
- webhook: forged/garbage signatures → 400 with zero writes; valid events
  flip `user_profiles.plan` by Price-id mapping; unknown prices and
  unknown customers are ignored; non-entitled statuses and deletions
  downgrade to 'free'; unset signing secret → 503.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import get_current_user_id, get_supabase
from app.main import app
from app.routers import billing

_UID = "00000000-0000-0000-0000-000000000042"
_WHSEC = "whsec_test_secret_for_billing_tests"
_STARTER_PRICE = "price_starter_test"
_PRO_PRICE = "price_pro_test"


@pytest.fixture
def saas_billing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "saas")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_webhook_secret", _WHSEC)
    monkeypatch.setattr(settings, "stripe_starter_price_id", _STARTER_PRICE)
    monkeypatch.setattr(settings, "stripe_pro_price_id", _PRO_PRICE)
    monkeypatch.setattr(settings, "next_app_url", "https://app.example")


@pytest.fixture
def sb() -> MagicMock:
    fake = MagicMock(name="supabase")
    fake.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
    yield fake
    app.dependency_overrides.clear()


def _client(sb: MagicMock) -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: sb
    app.dependency_overrides[get_current_user_id] = lambda: _UID
    return TestClient(app)


def _plan_updates(sb: MagicMock) -> list[dict[str, Any]]:
    return [c.args[0] for c in sb.table.return_value.update.call_args_list if "plan" in c.args[0]]


def _signed(payload: dict[str, Any], secret: str = _WHSEC) -> tuple[bytes, str]:
    """Sign a payload exactly the way Stripe does (t + HMAC-SHA256 v1)."""
    body = json.dumps(payload).encode()
    ts = int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={mac}"


def _sub_event(
    etype: str = "customer.subscription.updated",
    *,
    price: str = _PRO_PRICE,
    status: str = "active",
    user_meta: str | None = _UID,
    customer: str = "cus_x",
) -> dict[str, Any]:
    return {
        "id": "evt_1",
        "object": "event",
        "type": etype,
        "data": {
            "object": {
                "object": "subscription",
                "customer": customer,
                "status": status,
                "metadata": {"user_id": user_meta} if user_meta else {},
                "items": {"data": [{"price": {"id": price}}]},
            }
        },
    }


# ---- perimeter --------------------------------------------------------------


def test_billing_routes_404_outside_saas(sb: MagicMock) -> None:
    """Negative: default settings (self_host, no key) — the surface does
    not exist, including for authenticated callers."""
    client = _client(sb)
    assert client.post("/billing/checkout-session", json={"plan": "pro"}).status_code == 404
    assert client.post("/billing/portal-session").status_code == 404
    assert client.post("/billing/webhook", content=b"{}").status_code == 404


# ---- checkout / portal -------------------------------------------------------


def test_checkout_creates_customer_and_returns_url(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    stripe_client = MagicMock()
    stripe_client.customers.create.return_value = MagicMock(id="cus_new")
    stripe_client.checkout.sessions.create.return_value = MagicMock(
        url="https://checkout.stripe.com/s/1"
    )
    monkeypatch.setattr(billing, "_client", lambda: stripe_client)

    r = _client(sb).post("/billing/checkout-session", json={"plan": "starter"})

    assert r.status_code == 200
    assert r.json() == {"url": "https://checkout.stripe.com/s/1"}
    # New customer created (no stored id) and persisted.
    stripe_client.customers.create.assert_called_once()
    saved = [
        c.args[0]
        for c in sb.table.return_value.update.call_args_list
        if "stripe_customer_id" in c.args[0]
    ]
    assert saved == [{"stripe_customer_id": "cus_new"}]
    # The session carries the plan's price and the user stamp.
    params = stripe_client.checkout.sessions.create.call_args.kwargs["params"]
    assert params["line_items"] == [{"price": _STARTER_PRICE, "quantity": 1}]
    assert params["client_reference_id"] == _UID
    assert params["subscription_data"]["metadata"]["user_id"] == _UID
    assert params["mode"] == "subscription"


def test_checkout_reuses_existing_customer(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"stripe_customer_id": "cus_old"}
    ]
    stripe_client = MagicMock()
    stripe_client.checkout.sessions.create.return_value = MagicMock(url="https://x")
    monkeypatch.setattr(billing, "_client", lambda: stripe_client)

    r = _client(sb).post("/billing/checkout-session", json={"plan": "pro"})

    assert r.status_code == 200
    stripe_client.customers.create.assert_not_called()
    params = stripe_client.checkout.sessions.create.call_args.kwargs["params"]
    assert params["customer"] == "cus_old"


def test_checkout_unconfigured_price_is_503(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: a plan without a configured Price id can't be sold."""
    monkeypatch.setattr(settings, "stripe_starter_price_id", "")
    r = _client(sb).post("/billing/checkout-session", json={"plan": "starter"})
    assert r.status_code == 503


def test_portal_requires_existing_customer(saas_billing: None, sb: MagicMock) -> None:
    """Negative: no billing account yet → 409, no Stripe call."""
    r = _client(sb).post("/billing/portal-session")
    assert r.status_code == 409


# ---- webhook ----------------------------------------------------------------


def test_webhook_forged_signature_is_400_and_writes_nothing(
    saas_billing: None, sb: MagicMock
) -> None:
    """Negative: a valid-looking event signed with the WRONG secret is
    refused by the real verification code — and nothing is written."""
    body, sig = _signed(_sub_event(), secret="whsec_attacker")
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 400
    sb.table.return_value.update.assert_not_called()


def test_webhook_garbage_body_is_400(saas_billing: None, sb: MagicMock) -> None:
    r = _client(sb).post(
        "/billing/webhook",
        content=b"not json at all",
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert r.status_code == 400


def test_webhook_unset_secret_is_503(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: never process billing events unsigned."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    body, sig = _signed(_sub_event())
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 503


def test_webhook_active_subscription_sets_plan_by_price(saas_billing: None, sb: MagicMock) -> None:
    body, sig = _signed(_sub_event(price=_PRO_PRICE, status="active"))
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert _plan_updates(sb) == [{"plan": "pro"}]


def test_webhook_non_entitled_status_downgrades_to_free(saas_billing: None, sb: MagicMock) -> None:
    """Negative-direction: past_due never keeps a paid tier."""
    body, sig = _signed(_sub_event(status="past_due"))
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert _plan_updates(sb) == [{"plan": "free"}]


def test_webhook_subscription_deleted_downgrades_to_free(saas_billing: None, sb: MagicMock) -> None:
    body, sig = _signed(_sub_event("customer.subscription.deleted"))
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert _plan_updates(sb) == [{"plan": "free"}]


def test_webhook_unknown_price_is_ignored(saas_billing: None, sb: MagicMock) -> None:
    """Negative: an unmapped price must never grant or revoke a tier."""
    body, sig = _signed(_sub_event(price="price_someone_elses"))
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert _plan_updates(sb) == []


def test_webhook_unknown_customer_is_ignored(saas_billing: None, sb: MagicMock) -> None:
    """Negative: no metadata user + no profile match → no write."""
    body, sig = _signed(_sub_event(user_meta=None, customer="cus_stranger"))
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200
    assert _plan_updates(sb) == []


def test_webhook_checkout_completed_links_customer(saas_billing: None, sb: MagicMock) -> None:
    body, sig = _signed(
        {
            "id": "evt_2",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "object": "checkout.session",
                    "client_reference_id": _UID,
                    "customer": "cus_linked",
                }
            },
        }
    )
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert r.status_code == 200
    saved = [
        c.args[0]
        for c in sb.table.return_value.update.call_args_list
        if "stripe_customer_id" in c.args[0]
    ]
    assert saved == [{"stripe_customer_id": "cus_linked"}]


# ---- GET /billing/account ----------------------------------------------------


def test_billing_account_404_outside_saas(sb: MagicMock) -> None:
    assert _client(sb).get("/billing/account").status_code == 404


def test_billing_account_reports_plan_and_state(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.keys import store as keys_store
    from app.services.llm import budget as budget_mod

    monkeypatch.setattr(
        budget_mod,
        "get_llm_account",
        lambda supabase, *, user_id: budget_mod.LlmAccount(None, True, "starter"),
    )
    monkeypatch.setattr(keys_store, "has_usable_key", lambda s, *, user_id, provider: True)
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"stripe_customer_id": "cus_x"}
    ]

    r = _client(sb).get("/billing/account")

    assert r.status_code == 200
    assert r.json() == {
        "plan": "starter",
        "has_billing_account": True,
        "byok": True,
    }


def test_billing_account_defaults_free_no_account(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.keys import store as keys_store
    from app.services.llm import budget as budget_mod

    monkeypatch.setattr(
        budget_mod,
        "get_llm_account",
        lambda supabase, *, user_id: budget_mod.LlmAccount(None, True, None),
    )
    monkeypatch.setattr(keys_store, "has_usable_key", lambda s, *, user_id, provider: False)

    r = _client(sb).get("/billing/account")

    assert r.status_code == 200
    assert r.json() == {
        "plan": "free",
        "has_billing_account": False,
        "byok": False,
    }


def test_unset_app_url_is_503_not_stripe_500(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative (#210 release-drive bug): NEXT_APP_URL unset must be a
    clear 503 from OUR guard — never a relative URL sent to Stripe (which
    500s back as "Not a valid URL"). Checked before any Stripe call."""
    monkeypatch.setattr(settings, "next_app_url", "")
    stripe_client = MagicMock(name="stripe")
    monkeypatch.setattr(billing, "_client", lambda: stripe_client)

    r = _client(sb).post("/billing/checkout-session", json={"plan": "pro"})
    assert r.status_code == 503
    assert "NEXT_APP_URL" in r.json()["detail"]
    stripe_client.checkout.sessions.create.assert_not_called()

    # Portal has the same dependency (return_url).
    sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"stripe_customer_id": "cus_x"}
    ]
    r2 = _client(sb).post("/billing/portal-session")
    assert r2.status_code == 503
    stripe_client.billing_portal.sessions.create.assert_not_called()
