"""Stripe billing (Phase 3 slice 3; saas mode only).

Three routes:
- ``POST /billing/checkout-session`` — start a subscription purchase for a
  managed tier; returns the hosted Checkout URL.
- ``POST /billing/portal-session`` — open the hosted Customer Portal
  (change plan, update card, cancel); returns its URL.
- ``POST /billing/webhook`` — Stripe→us event sink; signature-verified,
  syncs ``user_profiles.plan`` from subscription state.

Design: no card data or custom payment UI ever touches this codebase —
Checkout and the Portal are Stripe-hosted. The webhook is the single
writer of plan state from billing events; the mapping is Price id →
plan, and UNKNOWN prices are ignored (logged), never guessed. Downgrade
to 'free' happens on subscription deletion or a non-active status —
'free' still works (BYOK), so a failed card never bricks an account.

All routes 404 outside saas mode / without a configured key
(``require_billing``): a self-hosted instance has no subscriptions and
must not even reveal the surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal, cast

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from supabase import Client

from app.config import settings
from app.dependencies import get_current_user_id, get_supabase
from app.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


def require_billing() -> None:
    """404 unless this instance actually sells subscriptions."""
    if settings.deployment_mode != "saas" or not settings.stripe_secret_key:
        raise HTTPException(status_code=404, detail="Not found")


def _client() -> stripe.StripeClient:
    return stripe.StripeClient(settings.stripe_secret_key)


def _price_for_plan(plan: str) -> str:
    price = {
        "starter": settings.stripe_starter_price_id,
        "pro": settings.stripe_pro_price_id,
    }.get(plan, "")
    if not price:
        raise HTTPException(
            status_code=503,
            detail=f"Billing for the {plan} plan is not configured.",
        )
    return price


def _plan_for_price(price_id: str) -> str | None:
    if price_id and price_id == settings.stripe_starter_price_id:
        return "starter"
    if price_id and price_id == settings.stripe_pro_price_id:
        return "pro"
    return None


def _get_stripe_customer_id(supabase: Client, user_id: str) -> str | None:
    rows = cast(
        list[dict[str, Any]],
        supabase.table("user_profiles")
        .select("stripe_customer_id")
        .eq("user_id", user_id)
        .execute()
        .data
        or [],
    )
    return cast("str | None", rows[0].get("stripe_customer_id")) if rows else None


def _save_stripe_customer_id(
    supabase: Client, user_id: str, customer_id: str
) -> None:
    supabase.table("user_profiles").update(
        {"stripe_customer_id": customer_id}
    ).eq("user_id", user_id).execute()


def _ensure_customer(supabase: Client, user_id: str) -> str:
    """The user's Stripe customer id, creating the customer on first use."""
    existing = _get_stripe_customer_id(supabase, user_id)
    if existing:
        return existing
    email: str | None = None
    try:
        resp = supabase.auth.admin.get_user_by_id(user_id)
        email = getattr(resp.user, "email", None)
    except Exception:  # pragma: no cover — email is a nice-to-have
        logger.warning("could not resolve email for user=%s", user_id)
    params: dict[str, Any] = {"metadata": {"user_id": user_id}}
    if email:
        params["email"] = email
    customer = _client().customers.create(params=cast(Any, params))
    _save_stripe_customer_id(supabase, user_id, customer.id)
    return customer.id


class CheckoutRequest(BaseModel):
    plan: Literal["starter", "pro"]


class BillingUrlResponse(BaseModel):
    url: str


# Sync `def`: the Stripe SDK and supabase-py are blocking; FastAPI's
# threadpool keeps them off the event loop (#107).
@router.post(
    "/checkout-session",
    response_model=BillingUrlResponse,
    dependencies=[Depends(require_billing)],
)
@limiter.limit("10/minute")
def create_checkout_session(
    request: Request,
    body: CheckoutRequest,
    user_id: str = Depends(get_current_user_id),
    supabase: Client = Depends(get_supabase),
) -> BillingUrlResponse:
    """Hosted-Checkout URL for subscribing to a managed tier."""
    price = _price_for_plan(body.plan)
    customer_id = _ensure_customer(supabase, user_id)
    session = _client().checkout.sessions.create(
        params={
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [{"price": price, "quantity": 1}],
            "success_url": f"{settings.next_app_url}/settings?billing=success",
            "cancel_url": f"{settings.next_app_url}/settings?billing=cancelled",
            "client_reference_id": user_id,
            # Stamped onto the subscription so every webhook event carries
            # the user id — no reverse lookup needed on the hot path.
            "subscription_data": {"metadata": {"user_id": user_id}},
        }
    )
    if not session.url:  # pragma: no cover — Stripe always returns one
        raise HTTPException(status_code=502, detail="Stripe returned no URL")
    return BillingUrlResponse(url=session.url)


# Sync `def`: blocking SDK + supabase work in the threadpool (#107).
@router.post(
    "/portal-session",
    response_model=BillingUrlResponse,
    dependencies=[Depends(require_billing)],
)
@limiter.limit("10/minute")
def create_portal_session(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    supabase: Client = Depends(get_supabase),
) -> BillingUrlResponse:
    """Hosted Customer-Portal URL (manage plan / card / cancel)."""
    customer_id = _get_stripe_customer_id(supabase, user_id)
    if not customer_id:
        raise HTTPException(
            status_code=409,
            detail="No billing account yet — subscribe to a plan first.",
        )
    session = _client().billing_portal.sessions.create(
        params={
            "customer": customer_id,
            "return_url": f"{settings.next_app_url}/settings",
        }
    )
    return BillingUrlResponse(url=session.url)


def _resolve_user_id(
    supabase: Client, *, metadata_user_id: str | None, customer_id: str | None
) -> str | None:
    """Subscription metadata first (we stamp it), customer lookup second."""
    if metadata_user_id:
        return metadata_user_id
    if not customer_id:
        return None
    rows = cast(
        list[dict[str, Any]],
        supabase.table("user_profiles")
        .select("user_id")
        .eq("stripe_customer_id", customer_id)
        .execute()
        .data
        or [],
    )
    return cast(str, rows[0]["user_id"]) if rows else None


def _set_plan(supabase: Client, user_id: str, plan: str) -> None:
    supabase.table("user_profiles").update({"plan": plan}).eq(
        "user_id", user_id
    ).execute()
    logger.info("billing: plan=%s user=%s", plan, user_id)


# Statuses whose subscription actually entitles the managed tier. Anything
# else (past_due, unpaid, canceled, incomplete, incomplete_expired, paused)
# falls back to 'free' — which still works via BYOK, so a failed card
# never bricks the account.
_ENTITLED_STATUSES = ("active", "trialing")


def _handle_event(supabase: Client, event: dict[str, Any]) -> None:
    etype = cast(str, event.get("type") or "")
    obj = cast(dict[str, Any], (event.get("data") or {}).get("object") or {})

    if etype == "checkout.session.completed":
        # Re-assert the customer link (idempotent) — the session is the
        # first moment Stripe tells us the pairing survived checkout. The
        # plan flip itself rides the subscription events that follow.
        user_id = cast("str | None", obj.get("client_reference_id"))
        customer_id = cast("str | None", obj.get("customer"))
        if user_id and customer_id:
            _save_stripe_customer_id(supabase, user_id, customer_id)
        return

    if etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        items = cast(dict[str, Any], obj.get("items") or {})
        first = (cast(list[Any], items.get("data") or []) or [{}])[0]
        price_id = cast(
            str, (cast(dict[str, Any], first).get("price") or {}).get("id") or ""
        )
        metadata = cast(dict[str, Any], obj.get("metadata") or {})
        user_id = _resolve_user_id(
            supabase,
            metadata_user_id=cast("str | None", metadata.get("user_id")),
            customer_id=cast("str | None", obj.get("customer")),
        )
        if user_id is None:
            logger.warning(
                "billing: %s for unknown customer=%s — ignored",
                etype,
                obj.get("customer"),
            )
            return

        if etype == "customer.subscription.deleted":
            _set_plan(supabase, user_id, "free")
            return

        status = cast(str, obj.get("status") or "")
        if status not in _ENTITLED_STATUSES:
            _set_plan(supabase, user_id, "free")
            return

        plan = _plan_for_price(price_id)
        if plan is None:
            # Never guess from an unknown price — a mis-mapped event must
            # not grant or revoke a tier.
            logger.warning(
                "billing: %s with unmapped price=%s for user=%s — ignored",
                etype,
                price_id,
                user_id,
            )
            return
        _set_plan(supabase, user_id, plan)
        return

    logger.debug("billing: ignored event type=%s", etype)


# Async `def`: needs the raw request body for signature verification; the
# blocking handler work is offloaded to the threadpool (#107). NO auth
# dependency — Stripe is the caller; the HMAC signature IS the auth.
@router.post("/webhook", dependencies=[Depends(require_billing)])
@limiter.limit("120/minute")
async def stripe_webhook(
    request: Request,
    supabase: Client = Depends(get_supabase),
) -> dict[str, bool]:
    if not settings.stripe_webhook_secret:
        # Refuse everything rather than process unsigned events.
        raise HTTPException(status_code=503, detail="Webhook not configured")
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        # Verification only — the handler below works on the (identical,
        # now-authenticated) raw JSON rather than stripe's typed objects.
        stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
            payload, signature, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid signature") from exc

    event = cast("dict[str, Any]", json.loads(payload))
    await asyncio.to_thread(_handle_event, supabase, event)
    return {"received": True}
