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
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import get_async_service_supabase, get_current_user_id
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
    # The billing-local helpers now run on the async service client and await
    # ``.execute()`` — make the terminal calls awaitable (AsyncMock).
    fake.table.return_value.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[])
    )
    fake.table.return_value.update.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[])
    )
    # auth-admin is async now (awaited in ``_ensure_customer``).
    fake.auth.admin.get_user_by_id = AsyncMock(return_value=MagicMock(user=MagicMock(email=None)))
    yield fake
    app.dependency_overrides.clear()


def _client(sb: MagicMock) -> TestClient:
    # Every billing route (incl. ``get_billing_account`` since #57 PR-G2c) now
    # runs on the async service client — override it + the current-user dep.
    app.dependency_overrides[get_async_service_supabase] = lambda: sb
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
    sb.table.return_value.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"stripe_customer_id": "cus_old"}])
    )
    stripe_client = MagicMock()
    stripe_client.checkout.sessions.create.return_value = MagicMock(url="https://x")
    monkeypatch.setattr(billing, "_client", lambda: stripe_client)

    r = _client(sb).post("/billing/checkout-session", json={"plan": "pro"})

    assert r.status_code == 200
    stripe_client.customers.create.assert_not_called()
    params = stripe_client.checkout.sessions.create.call_args.kwargs["params"]
    assert params["customer"] == "cus_old"


def test_checkout_defaults_to_returning_to_settings(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body without ``return_to`` keeps the pre-#887 destination.

    Asserted explicitly because the settings card still posts the old body
    shape — a default that drifted would silently reroute every purchase
    made from Settings into the onboarding wizard.
    """
    stripe_client = MagicMock()
    stripe_client.checkout.sessions.create.return_value = MagicMock(url="https://x")
    monkeypatch.setattr(billing, "_client", lambda: stripe_client)

    r = _client(sb).post("/billing/checkout-session", json={"plan": "starter"})

    assert r.status_code == 200
    params = stripe_client.checkout.sessions.create.call_args.kwargs["params"]
    assert params["success_url"] == "https://app.example/settings?billing=success"
    assert params["cancel_url"] == "https://app.example/settings?billing=cancelled"


def test_checkout_returns_to_onboarding_when_asked(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#887: paying from the wizard hands the user back to the wizard.

    Both URLs are checked. Only ``success_url`` moving would strand a user
    who cancels on a Settings page they never chose to visit.
    """
    stripe_client = MagicMock()
    stripe_client.checkout.sessions.create.return_value = MagicMock(url="https://x")
    monkeypatch.setattr(billing, "_client", lambda: stripe_client)

    r = _client(sb).post(
        "/billing/checkout-session",
        json={"plan": "starter", "return_to": "onboarding"},
    )

    assert r.status_code == 200
    params = stripe_client.checkout.sessions.create.call_args.kwargs["params"]
    assert params["success_url"] == "https://app.example/onboarding?billing=success"
    assert params["cancel_url"] == "https://app.example/onboarding?billing=cancelled"


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/phish",
        "//evil.example",
        "/settings/../../evil",
        "Settings",  # a valid key in the wrong case is still not a key
        "onboarding ",  # trailing space — no silent trim into a valid member
        "",
    ],
)
def test_checkout_refuses_a_caller_supplied_destination(
    saas_billing: None,
    sb: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    hostile: str,
) -> None:
    """The security property behind ``return_to`` being an enum (#887).

    Stripe redirects to whatever ``success_url`` says, so a caller-supplied
    destination is an open redirect wearing a payment confirmation — about
    the most credible phishing hop available. The check that matters is that
    NO Stripe session is created: a 422 that still charged someone would be
    the worst of both.
    """
    stripe_client = MagicMock()
    monkeypatch.setattr(billing, "_client", lambda: stripe_client)

    r = _client(sb).post(
        "/billing/checkout-session",
        json={"plan": "starter", "return_to": hostile},
    )

    assert r.status_code == 422
    stripe_client.checkout.sessions.create.assert_not_called()
    stripe_client.customers.create.assert_not_called()


def test_return_paths_are_relative_and_cannot_leave_the_app(
    saas_billing: None,
) -> None:
    """Guards the lookup table itself, not just the request parsing.

    The enum is only as safe as what it maps to: a future entry written as
    a full URL, or one that starts with ``//``, would reintroduce the open
    redirect from behind the validation rather than through it.
    """
    for key, path in billing._RETURN_PATHS.items():
        assert path.startswith("/"), key
        assert not path.startswith("//"), key
        assert "://" not in path, key
        assert ".." not in path, key


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


def test_webhook_cancel_at_period_end_keeps_access_until_the_period_ends(
    saas_billing: None, sb: MagicMock
) -> None:
    """The Terms promise it, so pin it (#439 legal review, flow A).

    "Cancelling stops future charges and takes effect at the end of the billing
    period you have already paid for — you keep access until then." Stripe
    models that as ``cancel_at_period_end=true`` with the status STILL
    ``active``; it only becomes ``canceled`` when the period actually ends.

    So the property under test is that we key entitlement on **status**, not on
    the cancellation flag. Reading ``cancel_at_period_end`` and downgrading on
    it would revoke access the moment someone cancels — taking away time they
    have already paid for, and contradicting the Terms.
    """
    event = _sub_event(status="active")
    event["data"]["object"]["cancel_at_period_end"] = True

    body, sig = _signed(event)
    r = _client(sb).post("/billing/webhook", content=body, headers={"stripe-signature": sig})

    assert r.status_code == 200
    assert _plan_updates(sb) == [{"plan": "pro"}]


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

    # The handler now awaits the async twins (#57 PR-G2c) on the async service
    # client — patch those, not the sync holdouts.
    monkeypatch.setattr(
        budget_mod,
        "get_llm_account_async",
        AsyncMock(return_value=budget_mod.LlmAccount(None, True, "starter")),
    )
    monkeypatch.setattr(keys_store, "has_usable_key_async", AsyncMock(return_value=True))
    sb.table.return_value.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"stripe_customer_id": "cus_x"}])
    )

    r = _client(sb).get("/billing/account")

    assert r.status_code == 200
    assert r.json() == {
        "plan": "starter",
        "has_billing_account": True,
        "byok": True,
        # #858: server capability, distinct from the user's byok state above;
        # the test env has no BYOK_MASTER_KEY, mirroring prod saas.
        "byok_available": False,
        # #867: from the enforcement resolver — a usable key means the USER
        # pays, whatever the plan says.
        "key_source": "user",
    }


def test_billing_account_defaults_free_no_account(
    saas_billing: None, sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.keys import store as keys_store
    from app.services.llm import budget as budget_mod

    monkeypatch.setattr(
        budget_mod,
        "get_llm_account_async",
        AsyncMock(return_value=budget_mod.LlmAccount(None, True, None)),
    )
    monkeypatch.setattr(keys_store, "has_usable_key_async", AsyncMock(return_value=False))

    r = _client(sb).get("/billing/account")

    assert r.status_code == 200
    assert r.json() == {
        "plan": "free",
        "has_billing_account": False,
        "byok": False,
        "byok_available": False,
        # #867: saas free with no usable key is the payer-LESS state — the
        # cost line must never call this an allowance.
        "key_source": "none",
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
    sb.table.return_value.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[{"stripe_customer_id": "cus_x"}])
    )
    r2 = _client(sb).post("/billing/portal-session")
    assert r2.status_code == 503
    stripe_client.billing_portal.sessions.create.assert_not_called()
