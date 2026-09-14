"""Price → plan mapping and entitlement statuses, shared by every reader.

WHY THIS IS ITS OWN MODULE
Two things now decide what a Stripe subscription entitles: the webhook
(`routers/billing.py`) and the reconciliation sweep (`services/billing_reconcile.py`).
If they disagree about what `price_X` means, or about which statuses count, the
sweep will "correct" the webhook's writes or vice versa — an oscillation that
looks like a billing bug and is nearly impossible to reason about from logs.

So there is one definition. The sweep is not a second opinion about
entitlement; it is the same opinion applied to a different input (Stripe's
current state rather than a delivered event).
"""

from __future__ import annotations

from typing import Final

from app.config import settings

# Statuses whose subscription actually entitles the managed tier. Anything
# else (past_due, unpaid, canceled, incomplete, incomplete_expired, paused)
# falls back to 'free' — which still works via BYOK, so a failed card never
# bricks the account.
ENTITLED_STATUSES: Final[tuple[str, ...]] = ("active", "trialing")

# Ordered weakest → strongest. Used by the reconciliation sweep to decide
# whether a correction is an UPGRADE (safe to apply automatically, because
# Stripe says it was paid for) or a DOWNGRADE (never applied automatically —
# a comped account is indistinguishable from a lapsed one from here).
#
# `trial` sits above `free` and below the paid tiers: it is a managed tier, so
# losing it matters, but any paid subscription supersedes it.
PLAN_RANK: Final[dict[str, int]] = {"free": 0, "trial": 1, "starter": 2, "pro": 3}


def plan_for_price(price_id: str) -> str | None:
    """The plan a price entitles, or None if the price is not one of ours.

    None is a refusal, not a default. A mis-mapped or unknown price must never
    grant or revoke a tier — the caller is expected to skip and say so.
    """
    if price_id and price_id == settings.stripe_starter_price_id:
        return "starter"
    if price_id and price_id == settings.stripe_pro_price_id:
        return "pro"
    return None


def rank(plan: str | None) -> int:
    """Rank of a plan name; unknown names rank lowest.

    Unknown ranks 0 rather than raising so a plan value this code has not
    heard of cannot cause the sweep to crash — but it also means an unknown
    plan is treated as the weakest, so the sweep could try to "upgrade" it.
    Callers that write must therefore check the TARGET is a known paid plan,
    not merely that the ranks differ.
    """
    return PLAN_RANK.get(plan or "free", 0)
