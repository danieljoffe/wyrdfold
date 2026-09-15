"""Catch billing drift: accounts Stripe says are paid that we still treat as free.

WHY THIS EXISTS
A webhook that never arrives is invisible. Checkout succeeds, the card is
charged, `stripe_customer_id` is written by `_ensure_customer` so billing even
*looks* connected — and `user_profiles.plan` never moves, so every AI feature
keeps returning 402. The customer paid and got nothing, and nothing in the
system notices.

That is not hypothetical. On 2026-09-14 this project had no live-mode webhook
endpoint at all, and the only reason it was caught is that someone went
looking. The failure modes are many and they all present identically:

  * no endpoint registered for the mode the keys are in
  * a signing secret that does not match the endpoint (every delivery 400s)
  * a Stripe outage, or deliveries that 500'd and exhausted their retries
  * a deploy window that dropped events

Rather than guard each one, this compares against the system that actually
holds the money. Stripe is authoritative: it is the thing that took payment.

WHAT IT DOES NOT DO — IT NEVER DOWNGRADES
Drift has two directions and they are not symmetric:

  Stripe says paid, we say free  -> they paid and got nothing. HEALED.
  Stripe says nothing, we say paid -> could be a comp. REPORTED ONLY.

Auto-revoking is unsafe here for a concrete reason: in this project's own
production data, 3 of 4 `pro` accounts have no Stripe customer at all. An
account comped by hand is indistinguishable from a lapsed one at this layer,
so revocation stays a human decision. The "never downgrade" property is
structural — writes require ``rank(target) > rank(current)`` AND a target that
is a known paid plan — not a conditional someone can forget.

ONE CAVEAT: SHARED STRIPE ACCOUNTS
The customer -> profile check assumes this database is the only one behind the
Stripe account/mode being read. If two environments share one (as staging and
production did on 2026-09-14, both on test keys), each will report the other's
customers as ``unknown_customer``. Separating modes — test for staging, live
for production — removes it; the log line says so, so nobody has to rediscover
it. The heal/report halves are unaffected: they key on a customer id this
database already stores.

This is NOT a second opinion about entitlement. It applies the same
``plan_for_price`` and ``ENTITLED_STATUSES`` the webhook uses, from
``services/billing_plans``, to a different input: Stripe's current state
instead of a delivered event. Two copies of that mapping would eventually
disagree, and the sweep and the webhook would overwrite each other forever.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from supabase._async.client import AsyncClient

from app.config import settings
from app.services.billing_plans import ENTITLED_STATUSES, plan_for_price, rank

logger = logging.getLogger(__name__)

# Stripe's maximum page size. Fewer round trips, and the sweep is O(pages)
# rather than O(users) — listing per customer would be one API call each,
# which is fine at six accounts and untenable at ten thousand.
_PAGE_SIZE = 100

# Hard stop on pagination. A list that never reports has_more=False (an API
# change, a pathological account) must not spin forever inside a scheduler
# tick holding the event loop.
_MAX_PAGES = 200


def _as_dict(obj: Any) -> dict[str, Any]:
    """A plain nested dict, whatever the SDK handed us.

    The Stripe SDK returns ``StripeObject`` instances, NOT dicts — `.get()`
    raises `AttributeError: 'get' is a dict method, but a Subscription is not
    a dict`. The webhook path never hits this because it parses raw JSON off
    the wire; this sweep is the first code here to touch SDK objects.

    Found by running the sweep against real Stripe after the unit tests were
    green: the fixtures returned dicts, so they encoded an assumption about the
    payload shape rather than the dependency's actual behaviour. The fakes now
    return non-dict objects for the same reason.

    ``to_dict_recursive`` because the fields we need are nested
    (``items.data[0].price.id``); a shallow conversion leaves StripeObjects
    underneath and the crash simply moves one level down.
    """
    if isinstance(obj, dict):
        return cast(dict[str, Any], obj)
    for method in ("to_dict_recursive", "to_dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            return cast(dict[str, Any], fn())
    return cast(dict[str, Any], dict(obj))


def _expected_plans(subscriptions: list[dict[str, Any]]) -> dict[str, str]:
    """``customer_id -> plan`` for every entitled subscription.

    A customer with two entitled subscriptions gets the STRONGEST of them.
    That is an anomaly worth logging, but erring upward is the safe direction:
    they are paying for both, and granting the lesser would be taking something
    away. Choosing by "most recent" instead would make the result depend on
    list ordering, which is not a property worth relying on.
    """
    best: dict[str, str] = {}
    for sub in subscriptions:
        if cast(str, sub.get("status") or "") not in ENTITLED_STATUSES:
            continue
        customer = sub.get("customer")
        if not isinstance(customer, str) or not customer:
            continue
        items = cast(dict[str, Any], sub.get("items") or {})
        data = cast(list[Any], items.get("data") or [])
        if not data:
            continue
        price_id = cast(str, (cast(dict[str, Any], data[0]).get("price") or {}).get("id") or "")
        plan = plan_for_price(price_id)
        if plan is None:
            # Never guess from an unknown price — same refusal the webhook
            # makes. A price we do not recognise must not grant a tier.
            logger.warning(
                "billing reconcile: subscription %s has unmapped price=%s — ignored",
                sub.get("id"),
                price_id,
            )
            continue
        current = best.get(customer)
        if current is None:
            best[customer] = plan
        elif current != plan:
            strongest = plan if rank(plan) > rank(current) else current
            logger.warning(
                "billing reconcile: customer=%s has multiple entitled plans "
                "(%s, %s) — taking %s",
                customer,
                current,
                plan,
                strongest,
            )
            best[customer] = strongest
    return best


def _list_all_subscriptions(client: Any) -> list[dict[str, Any]]:
    """Every subscription, paginated. Blocking — call via ``asyncio.to_thread``.

    ``status="all"`` on purpose: filtering server-side to active would hide the
    lapsed ones, and a customer whose subscription is `canceled` is exactly the
    case the report half needs to see.
    """
    out: list[dict[str, Any]] = []
    starting_after: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"limit": _PAGE_SIZE, "status": "all"}
        if starting_after:
            params["starting_after"] = starting_after
        page = client.subscriptions.list(params=params)
        rows = [_as_dict(s) for s in (page.data or [])]
        out.extend(rows)
        if not getattr(page, "has_more", False) or not rows:
            return out
        starting_after = cast(str, rows[-1].get("id") or "")
        if not starting_after:
            return out
    logger.error(
        "billing reconcile: pagination hit the %d-page cap — results are PARTIAL",
        _MAX_PAGES,
    )
    return out


def _entitled_plan_now(client: Any, customer_id: str) -> str | None:
    """Re-ask Stripe about ONE customer. Blocking — call via ``to_thread``.

    The bulk snapshot is unavoidably stale by the time a heal is written, and
    stale in the one direction that matters: it can say "paid" about a
    subscription cancelled since. Because the sweep never downgrades, writing
    from a stale snapshot does not merely produce a transient error — it
    restores paid access permanently, until a human notices.

    A compare-and-set on the profile row does NOT close this. If the
    cancellation lands BEFORE the profile read, the row already reads `free`,
    so the CAS sees no conflict and the stale grant goes through anyway.
    Freshness has to come from the authoritative side.

    Cheap because it only runs for accounts about to be healed, which is the
    rare path — the common case is `in_sync` and makes no extra call.
    """
    page = client.subscriptions.list(
        params={"customer": customer_id, "status": "all", "limit": _PAGE_SIZE}
    )
    rows = [_as_dict(sub) for sub in (page.data or [])]
    return _expected_plans(rows).get(customer_id)


async def reconcile_billing(
    supabase: AsyncClient, *, client: Any | None = None
) -> dict[str, int]:
    """Compare Stripe's subscriptions against stored plans. Heal up, report down.

    Best-effort and never raises: it runs from the scheduler, where an escaping
    exception is swallowed by APScheduler and the failure becomes invisible —
    the exact class of problem this sweep exists to prevent.
    """
    report = {
        "checked": 0,
        "in_sync": 0,
        "healed": 0,
        "underpaid_reported": 0,
        "unknown_customer": 0,
        "stale_skipped": 0,
    }

    # Billing is saas-only. A self-hosted instance has no Stripe relationship,
    # and must not make outbound calls on a timer because a flag was left on.
    if settings.deployment_mode != "saas":
        return report
    if not settings.stripe_secret_key:
        logger.warning("billing reconcile: no STRIPE_SECRET_KEY — skipped")
        return report

    try:
        if client is None:
            import stripe

            client = stripe.StripeClient(settings.stripe_secret_key)
        subscriptions = await asyncio.to_thread(_list_all_subscriptions, client)
    except Exception:
        logger.exception("billing reconcile: could not list subscriptions — skipped")
        return report

    expected = _expected_plans(subscriptions)

    try:
        resp = await (
            supabase.table("user_profiles")
            .select("user_id, plan, stripe_customer_id, updated_at")
            .not_.is_("stripe_customer_id", "null")
            .execute()
        )
        profiles = cast(list[dict[str, Any]], resp.data or [])
    except Exception:
        logger.exception("billing reconcile: could not read profiles — skipped")
        return report

    seen: set[str] = set()
    for row in profiles:
        customer = cast(str, row.get("stripe_customer_id") or "")
        if not customer:
            continue
        seen.add(customer)
        report["checked"] += 1
        current = cast("str | None", row.get("plan"))
        target = expected.get(customer)

        if target is None:
            # Stripe has no entitled subscription for them. Could be a lapse,
            # could be a comp, could be an abandoned checkout. Never act.
            #
            # Only a PAID tier is worth reporting here. `trial` is granted by
            # us and bounded by `trial_expired()`, not by Stripe — having no
            # subscription is its NORMAL state, not a discrepancy. Reporting it
            # was a false positive that scaled badly: `trial` is the default
            # plan for new users (the entitlements trigger sets it on INSERT),
            # so every trial user who opened a checkout and did not finish
            # would emit an ERROR every tick, forever. Alert noise is how a
            # real signal later gets ignored.
            #
            # `rank(current) > 0` was the original condition and is wrong for
            # exactly one value; naming the paid plans says what is meant.
            if current in ("starter", "pro"):
                report["underpaid_reported"] += 1
                logger.error(
                    "billing reconcile: user=%s is on plan=%s but Stripe has no "
                    "entitled subscription for customer=%s — NOT changed, needs a human",
                    row.get("user_id"),
                    current,
                    customer,
                )
            else:
                report["in_sync"] += 1
            continue

        if current == target:
            report["in_sync"] += 1
            continue

        # Upgrade only, and only to a plan we actually recognise as paid.
        # `rank` alone is not enough: an unknown stored plan ranks 0, so a
        # rank comparison would happily "upgrade" it to anything.
        if target in ("starter", "pro") and rank(target) > rank(current):
            # RE-ASK STRIPE before writing. `target` came from a snapshot taken
            # before the profile read, and a cancellation in between would make
            # this write restore paid access PERMANENTLY — the sweep never
            # downgrades, so nothing would ever take it back.
            try:
                fresh = await asyncio.to_thread(
                    _entitled_plan_now, client, customer
                )
            except Exception:
                logger.exception(
                    "billing reconcile: could not re-verify customer=%s — not healing",
                    customer,
                )
                continue
            if fresh != target:
                report["stale_skipped"] += 1
                logger.warning(
                    "billing reconcile: customer=%s changed between the snapshot "
                    "(%s) and now (%s) — NOT healed; the next pass will act on "
                    "fresh data",
                    customer,
                    target,
                    fresh,
                )
                continue
            # CONCURRENCY TOKEN, not a value comparison.
            #
            # `.eq("plan", current)` is not enough, and the hole is exact: the
            # cancellation webhook writes `free` — the SAME value we already
            # read — so a value-based CAS still matches and the stale grant
            # goes in. `updated_at` is bumped by trg_user_profiles_updated_at
            # (BEFORE UPDATE, `NEW.updated_at = NOW()`, unconditional), so ANY
            # intervening write moves it, including a same-value one. That is
            # what makes this interleaving detectable rather than invisible.
            token = row.get("updated_at")
            if not token:
                logger.error(
                    "billing reconcile: user=%s has no updated_at to lock on — "
                    "not healing (the CAS would be unguarded)",
                    row.get("user_id"),
                )
                continue
            try:
                resp = await (
                    supabase.table("user_profiles")
                    .update({"plan": target})
                    .eq("user_id", row.get("user_id"))
                    .eq("updated_at", token)
                    .execute()
                )
            except Exception:
                logger.exception(
                    "billing reconcile: failed to heal user=%s to plan=%s",
                    row.get("user_id"),
                    target,
                )
                continue
            # A conditional update that matched NOTHING is not a heal. Counting
            # it as one would report success for the exact interleaving this
            # lock exists to catch — the failure would look like a fix.
            if not (getattr(resp, "data", None) or []):
                report["stale_skipped"] += 1
                logger.warning(
                    "billing reconcile: user=%s changed underneath the sweep "
                    "(another writer touched the row) — NOT healed; the next "
                    "pass will act on fresh data",
                    row.get("user_id"),
                )
                continue
            report["healed"] += 1
            logger.error(
                "billing reconcile: HEALED user=%s %s -> %s (Stripe says paid; the "
                "webhook did not apply it — check webhook delivery)",
                row.get("user_id"),
                current,
                target,
            )
        else:
            report["underpaid_reported"] += 1
            logger.error(
                "billing reconcile: user=%s is on plan=%s but Stripe entitles %s — "
                "NOT changed (a downgrade is never automatic), needs a human",
                row.get("user_id"),
                current,
                target,
            )

    # Stripe customers with an entitled subscription and no profile here. Not
    # actionable from this side, but silence would hide a broken link between
    # a payment and an account.
    for customer in expected:
        if customer not in seen:
            report["unknown_customer"] += 1
            logger.error(
                "billing reconcile: Stripe customer=%s has an entitled subscription "
                "but no user_profiles row references it — a payment with no account "
                "attached, OR another environment sharing this Stripe account/mode",
                customer,
            )

    return report
