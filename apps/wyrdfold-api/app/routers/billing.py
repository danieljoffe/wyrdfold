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
from supabase import AsyncClient

from app.config import settings
from app.dependencies import get_async_service_supabase, get_current_user_id
from app.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


def require_billing() -> None:
    """404 unless this instance actually sells subscriptions."""
    if settings.deployment_mode != "saas" or not settings.stripe_secret_key:
        raise HTTPException(status_code=404, detail="Not found")


def _client() -> stripe.StripeClient:
    return stripe.StripeClient(settings.stripe_secret_key)


def _app_url() -> str:
    """The FE origin for Checkout/Portal redirect URLs.

    Guarded: Stripe rejects relative URLs, so an unset ``NEXT_APP_URL``
    must be a clear 503 here — not a 500 leaked from Stripe's "Not a
    valid URL" (bit prod on the #210 release drive: the var was set on
    Vercel but not Railway).
    """
    if not settings.next_app_url:
        raise HTTPException(
            status_code=503,
            detail="Billing is not fully configured (NEXT_APP_URL).",
        )
    return settings.next_app_url


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


async def _get_stripe_customer_id(supabase: AsyncClient, user_id: str) -> str | None:
    resp = await (
        supabase.table("user_profiles")
        .select("stripe_customer_id")
        .eq("user_id", user_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return cast("str | None", rows[0].get("stripe_customer_id")) if rows else None


async def _save_stripe_customer_id(supabase: AsyncClient, user_id: str, customer_id: str) -> None:
    await (
        supabase.table("user_profiles")
        .update({"stripe_customer_id": customer_id})
        .eq("user_id", user_id)
        .execute()
    )


async def _ensure_customer(supabase: AsyncClient, user_id: str) -> str:
    """The user's Stripe customer id, creating the customer on first use."""
    existing = await _get_stripe_customer_id(supabase, user_id)
    if existing:
        return existing
    email: str | None = None
    try:
        # Async auth-admin (the installed gotrue ships it as ``async def``).
        resp = await supabase.auth.admin.get_user_by_id(user_id)
        email = getattr(resp.user, "email", None)
    except Exception:  # pragma: no cover — email is a nice-to-have
        logger.warning("could not resolve email for user=%s", user_id)
    params: dict[str, Any] = {"metadata": {"user_id": user_id}}
    if email:
        params["email"] = email
    # Stripe SDK is synchronous — keep the blocking round-trip off the loop.
    customer = await asyncio.to_thread(lambda: _client().customers.create(params=cast(Any, params)))
    await _save_stripe_customer_id(supabase, user_id, customer.id)
    return customer.id


#: Where Checkout returns the user, keyed by ``CheckoutRequest.return_to``.
#:
#: Deliberately a server-side lookup off a closed enum rather than a path (or
#: URL) taken from the request. Stripe redirects to whatever ``success_url``
#: says, so honouring a caller-supplied value would hand an attacker an open
#: redirect with a payment confirmation attached to it — the most credible
#: possible landing page for a phishing hop. A caller can only name a key; it
#: cannot describe a destination.
_RETURN_PATHS: dict[str, str] = {
    "settings": "/settings",
    "onboarding": "/onboarding",
}


class CheckoutRequest(BaseModel):
    plan: Literal["starter", "pro"]
    #: Which surface the purchase started from, so Checkout can hand the user
    #: back to it (#887). Defaults to ``settings`` — the historical behaviour
    #: and the only caller before onboarding gained a subscribe step.
    return_to: Literal["settings", "onboarding"] = "settings"


class BillingUrlResponse(BaseModel):
    url: str


class BillingAccountResponse(BaseModel):
    plan: str
    #: Whether a Stripe customer exists — drives "Upgrade" vs "Manage
    #: subscription" in the settings card.
    has_billing_account: bool
    #: The user's own usable OpenRouter key pays inference (no managed
    #: quota applies) — drives the "your key pays" presentation.
    #: (byok_available below is the SERVER capability; this is the user state.)
    byok: bool
    #: Whether this server offers BYOK at all (#858) — the free-plan copy
    #: must not say "add one above" when the key fields are disabled.
    byok_available: bool
    #: Who pays when this user spends (#867/#991) — the SAME resolution the
    #: budget gates enforce (ResolvedQuota.key_source), not reconstructed
    #: from booleans: "byok: false" alone conflates a managed payer with the
    #: payer-less saas-free state, which is how a canceled subscriber almost
    #: got told historical generations "used your monthly allowance".
    key_source: Literal["host", "user", "none"]


# `async def` (#57 PR-G2c): every read runs on the pooled async service client —
# the billing-local ``_get_stripe_customer_id`` plus the cross-service
# ``budget.get_llm_account_async`` and ``keys_store.has_usable_key_async`` twins
# (added in G2c). The sync ``get_llm_account`` / ``has_usable_key`` stay for their
# other callers (the sync budget resolver + LLM-client factory); this handler no
# longer touches the sync service client.
@router.get(
    "/account",
    response_model=BillingAccountResponse,
    dependencies=[Depends(require_billing)],
)
async def get_billing_account(
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> BillingAccountResponse:
    """The settings card's read: plan + billing/BYOK state in one call."""
    from app.services import keys as keys_service
    from app.services.llm import budget

    account = await budget.get_llm_account_async(supabase, user_id=user_id)
    has_billing_account = (await _get_stripe_customer_id(supabase, user_id)) is not None
    # Payer identity comes from the enforcement resolver (#991), never
    # re-derived here. This endpoint is saas-only (require_billing), and in
    # saas key_source == "user" iff a usable BYOK key exists — so ``byok``
    # collapses to that equivalence and the standalone key check goes away.
    quota = await budget.resolve_llm_quota_async(supabase, user_id=user_id)
    return BillingAccountResponse(
        plan=account.plan or "free",
        has_billing_account=has_billing_account,
        byok=quota.key_source == "user",
        # #858: the free-plan copy branches on whether this server offers
        # BYOK at all — "add your own key" is a dead end when it doesn't.
        byok_available=keys_service.is_configured(),
        key_source=quota.key_source,
    )


# `async def` (#57 PR-G2a): ``_ensure_customer``'s read/write + ``auth.admin``
# call run on the async service client; the blocking Stripe SDK calls are driven
# off the loop via ``asyncio.to_thread`` (#107).
@router.post(
    "/checkout-session",
    response_model=BillingUrlResponse,
    dependencies=[Depends(require_billing)],
)
@limiter.limit("10/minute")
async def create_checkout_session(
    request: Request,
    body: CheckoutRequest,
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> BillingUrlResponse:
    """Hosted-Checkout URL for subscribing to a managed tier."""
    price = _price_for_plan(body.plan)
    app_url = _app_url()
    # Pydantic has already rejected anything outside the enum, so the lookup
    # cannot miss; the default keeps a body without ``return_to`` on the old
    # path rather than failing a request that used to work.
    return_path = _RETURN_PATHS.get(body.return_to, _RETURN_PATHS["settings"])
    customer_id = await _ensure_customer(supabase, user_id)
    session = await asyncio.to_thread(
        lambda: _client().checkout.sessions.create(
            params={
                "mode": "subscription",
                "customer": customer_id,
                "line_items": [{"price": price, "quantity": 1}],
                "success_url": f"{app_url}{return_path}?billing=success",
                "cancel_url": f"{app_url}{return_path}?billing=cancelled",
                "client_reference_id": user_id,
                # Stamped onto the subscription so every webhook event carries
                # the user id — no reverse lookup needed on the hot path.
                "subscription_data": {"metadata": {"user_id": user_id}},
            }
        )
    )
    if not session.url:  # pragma: no cover — Stripe always returns one
        raise HTTPException(status_code=502, detail="Stripe returned no URL")
    return BillingUrlResponse(url=session.url)


# `async def` (#57 PR-G2a): the billing-local ``_get_stripe_customer_id`` read
# runs on the async service client; the blocking Stripe SDK call is driven off
# the loop via ``asyncio.to_thread`` (#107).
@router.post(
    "/portal-session",
    response_model=BillingUrlResponse,
    dependencies=[Depends(require_billing)],
)
@limiter.limit("10/minute")
async def create_portal_session(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> BillingUrlResponse:
    """Hosted Customer-Portal URL (manage plan / card / cancel)."""
    customer_id = await _get_stripe_customer_id(supabase, user_id)
    if not customer_id:
        raise HTTPException(
            status_code=409,
            detail="No billing account yet — subscribe to a plan first.",
        )
    session = await asyncio.to_thread(
        lambda: _client().billing_portal.sessions.create(
            params={
                "customer": customer_id,
                "return_url": f"{_app_url()}/settings",
            }
        )
    )
    return BillingUrlResponse(url=session.url)


#: Subscription statuses that are still capable of billing someone, and so
#: must be cancelled before an account goes away. Everything else
#: (``canceled``, ``incomplete_expired``) is already terminal.
_CANCELLABLE_STATUSES = frozenset({"active", "trialing", "past_due", "unpaid", "paused"})


async def cancel_subscriptions_for_deletion(supabase: AsyncClient, user_id: str) -> list[str]:
    """Cancel the user's live subscriptions ahead of account deletion (#889).

    Returns the ids cancelled (empty when there was nothing to cancel, or when
    this instance doesn't sell subscriptions at all).

    **Call this BEFORE the deletion cascade.** ``stripe_customer_id`` lives on
    ``user_profiles``, which the cascade deletes — so after it runs there is no
    link left from the Stripe customer back to a person. That was the whole
    defect: the subscription kept billing, the webhook ignored its events as an
    unknown customer, and nobody could trace the charge afterwards.

    Cancels **immediately** rather than at period end. The account is being
    erased, so there is no access left to preserve for the remainder of the
    period — leaving a subscription "active" for a user who no longer exists
    would only produce another charge and another orphaned customer.

    Stripe errors propagate deliberately. The caller must refuse the deletion
    rather than swallow them: a deletion the user can retry is recoverable,
    whereas deleting first and failing to cancel reproduces exactly the
    untraceable-charge state this function exists to prevent.
    """
    if settings.deployment_mode != "saas" or not settings.stripe_secret_key:
        return []
    customer_id = await _get_stripe_customer_id(supabase, user_id)
    if not customer_id:
        return []

    client = _client()
    # ``status="all"`` so a past_due or paused subscription is caught too —
    # listing only "active" would leave a delinquent subscription billing.
    subs = await asyncio.to_thread(
        lambda: client.subscriptions.list(
            params={"customer": customer_id, "status": "all", "limit": 100}
        )
    )
    cancelled: list[str] = []
    for sub in subs.data or []:
        status = cast(str, getattr(sub, "status", "") or "")
        sub_id = cast(str, getattr(sub, "id", "") or "")
        if status not in _CANCELLABLE_STATUSES or not sub_id:
            continue
        await asyncio.to_thread(lambda sid=sub_id: client.subscriptions.cancel(sid))  # type: ignore[misc]
        cancelled.append(sub_id)
    if cancelled:
        logger.info(
            "cancelled %d subscription(s) for user=%s ahead of account deletion",
            len(cancelled),
            user_id,
        )
    return cancelled


async def _resolve_user_id(
    supabase: AsyncClient, *, metadata_user_id: str | None, customer_id: str | None
) -> str | None:
    """Subscription metadata first (we stamp it), customer lookup second."""
    if metadata_user_id:
        return metadata_user_id
    if not customer_id:
        return None
    resp = await (
        supabase.table("user_profiles")
        .select("user_id")
        .eq("stripe_customer_id", customer_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return cast(str, rows[0]["user_id"]) if rows else None


async def _set_plan(supabase: AsyncClient, user_id: str, plan: str) -> None:
    await supabase.table("user_profiles").update({"plan": plan}).eq("user_id", user_id).execute()
    logger.info("billing: plan=%s user=%s", plan, user_id)


# Statuses whose subscription actually entitles the managed tier. Anything
# else (past_due, unpaid, canceled, incomplete, incomplete_expired, paused)
# falls back to 'free' — which still works via BYOK, so a failed card
# never bricks the account.
_ENTITLED_STATUSES = ("active", "trialing")


async def _handle_event(supabase: AsyncClient, event: dict[str, Any]) -> None:
    etype = cast(str, event.get("type") or "")
    obj = cast(dict[str, Any], (event.get("data") or {}).get("object") or {})

    if etype == "checkout.session.completed":
        # Re-assert the customer link (idempotent) — the session is the
        # first moment Stripe tells us the pairing survived checkout. The
        # plan flip itself rides the subscription events that follow.
        user_id = cast("str | None", obj.get("client_reference_id"))
        customer_id = cast("str | None", obj.get("customer"))
        if user_id and customer_id:
            await _save_stripe_customer_id(supabase, user_id, customer_id)
        return

    if etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        items = cast(dict[str, Any], obj.get("items") or {})
        first = (cast(list[Any], items.get("data") or []) or [{}])[0]
        price_id = cast(str, (cast(dict[str, Any], first).get("price") or {}).get("id") or "")
        metadata = cast(dict[str, Any], obj.get("metadata") or {})
        user_id = await _resolve_user_id(
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
            await _set_plan(supabase, user_id, "free")
            return

        status = cast(str, obj.get("status") or "")
        if status not in _ENTITLED_STATUSES:
            await _set_plan(supabase, user_id, "free")
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
        await _set_plan(supabase, user_id, plan)
        return

    logger.debug("billing: ignored event type=%s", etype)


# Async `def`: needs the raw request body for signature verification. NO auth
# dependency — Stripe is the caller; the HMAC signature IS the auth. The DB work
# runs on the async service client (#57 PR-G2a): ``_handle_event`` is now an async
# coroutine awaited on the loop (its per-row writes yield), replacing the prior
# ``asyncio.to_thread`` offload of the sync client. ``construct_event`` is local
# HMAC verification (no I/O), so it stays inline exactly as before.
@router.post("/webhook", dependencies=[Depends(require_billing)])
@limiter.limit("120/minute")
async def stripe_webhook(
    request: Request,
    supabase: AsyncClient = Depends(get_async_service_supabase),
) -> dict[str, bool]:
    if not settings.stripe_webhook_secret:
        # Refuse everything rather than process unsigned events.
        raise HTTPException(status_code=503, detail="Webhook not configured")
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        # Verification only — the handler below works on the (identical,
        # now-authenticated) raw JSON rather than stripe's typed objects.
        stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid signature") from exc

    event = cast("dict[str, Any]", json.loads(payload))
    await _handle_event(supabase, event)
    return {"received": True}
