"""Guards for the billing reconciliation sweep (#861).

The sweep exists because a missed webhook is invisible: the card is charged,
`stripe_customer_id` is written, and `plan` never moves. These tests pin the
two properties that make it safe to run unattended — it heals the paid-but-free
case, and it can NEVER downgrade anyone.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.billing_reconcile import (
    _as_dict,
    _expected_plans,
    _list_all_subscriptions,
    reconcile_billing,
)

STARTER = "price_starter_test"
PRO = "price_pro_test"


@pytest.fixture(autouse=True)
def _billing_settings(monkeypatch):
    """Real-looking config: saas mode, a key, and the two mapped prices."""
    from app.config import settings

    monkeypatch.setattr(settings, "deployment_mode", "saas", raising=False)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "stripe_starter_price_id", STARTER, raising=False)
    monkeypatch.setattr(settings, "stripe_pro_price_id", PRO, raising=False)


class StripeObjectLike:
    """Behaves like the SDK's StripeObject: NOT a dict, no `.get()`.

    The first version of this fixture returned plain dicts, so every test
    passed while the real sweep crashed on live data with
    `AttributeError: 'get' is a dict method, but a Subscription is not a dict`.
    The fixture encoded an assumption about the payload instead of the
    dependency's behaviour, which is the one thing a fixture must not do.
    """

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def to_dict_recursive(self) -> dict[str, Any]:
        return self._data

    def __getattr__(self, name: str):
        # Mirrors the SDK: attribute access for real fields, and a pointed
        # failure for dict methods like `.get`.
        if name in ("get", "keys", "items", "values"):
            raise AttributeError(f"'{name}' is a dict method, but a Subscription is not a dict.")
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def sub(customer: str, price: str, status: str = "active", sid: str = "sub_1") -> Any:
    """A subscription shaped like Stripe's actual payload, in an SDK-like object."""
    return StripeObjectLike(
        {
            "id": sid,
            "customer": customer,
            "status": status,
            "items": {"data": [{"price": {"id": price}}]},
        }
    )


class FakeStripe:
    """Paginates like Stripe: `.data` + `.has_more`, cursor via starting_after."""

    def __init__(self, pages: list[list[dict[str, Any]]], *, explode: bool = False):
        self.pages = pages
        self.explode = explode
        self.calls: list[dict[str, Any]] = []
        self.subscriptions = self

    def list(self, params: dict[str, Any]):
        if self.explode:
            raise RuntimeError("stripe is down")
        self.calls.append(dict(params))
        after = params.get("starting_after")
        index = 0
        if after:
            for i, page in enumerate(self.pages):
                if any(s.id == after for s in page):
                    index = i + 1
                    break
        data = self.pages[index] if index < len(self.pages) else []
        has_more = index < len(self.pages) - 1

        class Page:
            pass

        p = Page()
        p.data = data
        p.has_more = has_more
        return p


class FakeDB:
    """Enough of the supabase chain to record what the sweep would write."""

    def __init__(
        self, rows: list[dict[str, Any]], *, read_fails: bool = False, write_fails: bool = False
    ):
        self.rows = rows
        self.read_fails = read_fails
        self.write_fails = write_fails
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self._update: dict[str, Any] | None = None

    def table(self, _name: str):
        return self

    def select(self, _cols: str):
        return self

    @property
    def not_(self):
        return self

    def is_(self, _col: str, _val: str):
        return self

    def update(self, payload: dict[str, Any]):
        self._update = payload
        return self

    def eq(self, _col: str, value: str):
        self._eq = value
        return self

    async def execute(self):
        if self._update is not None:
            if self.write_fails:
                self._update = None
                raise RuntimeError("write failed")
            self.writes.append((self._eq, dict(self._update)))
            self._update = None

            class R:
                pass

            r = R()
            r.data = []
            return r
        if self.read_fails:
            raise RuntimeError("read failed")

        class R2:
            pass

        r = R2()
        r.data = self.rows
        return r


def profile(user: str, plan: str, customer: str | None) -> dict[str, Any]:
    return {"user_id": user, "plan": plan, "stripe_customer_id": customer}


# --- the bug this exists for --------------------------------------------


@pytest.mark.asyncio
async def test_paid_but_free_is_healed() -> None:
    """The webhook-missed case: Stripe charged them, we still say free."""
    db = FakeDB([profile("u1", "free", "cus_1")])
    stripe = FakeStripe([[sub("cus_1", STARTER)]])
    report = await reconcile_billing(db, client=stripe)
    assert db.writes == [("u1", {"plan": "starter"})]
    assert report["healed"] == 1


@pytest.mark.asyncio
async def test_healing_is_idempotent() -> None:
    """A second pass over already-correct data must write nothing."""
    db = FakeDB([profile("u1", "starter", "cus_1")])
    stripe = FakeStripe([[sub("cus_1", STARTER)]])
    report = await reconcile_billing(db, client=stripe)
    assert db.writes == []
    assert report["in_sync"] == 1 and report["healed"] == 0


@pytest.mark.asyncio
async def test_trial_is_upgraded_to_a_paid_plan() -> None:
    """`trial` ranks below the paid tiers, so a real subscription supersedes it."""
    db = FakeDB([profile("u1", "trial", "cus_1")])
    report = await reconcile_billing(db, client=FakeStripe([[sub("cus_1", PRO)]]))
    assert db.writes == [("u1", {"plan": "pro"})]
    assert report["healed"] == 1


# --- the property that makes it safe: NEVER downgrade --------------------


@pytest.mark.asyncio
async def test_a_downgrade_is_reported_never_applied() -> None:
    """Stripe says starter, we say pro. Could be a comp. Must not be touched."""
    db = FakeDB([profile("u1", "pro", "cus_1")])
    report = await reconcile_billing(db, client=FakeStripe([[sub("cus_1", STARTER)]]))
    assert db.writes == [], "the sweep downgraded an account"
    assert report["underpaid_reported"] == 1


@pytest.mark.asyncio
async def test_no_subscription_at_all_never_downgrades() -> None:
    """A comped account has a customer id and no subscription. Leave it alone."""
    db = FakeDB([profile("u1", "pro", "cus_1")])
    report = await reconcile_billing(db, client=FakeStripe([[]]))
    assert db.writes == []
    assert report["underpaid_reported"] == 1


@pytest.mark.parametrize("status", ["canceled", "past_due", "unpaid", "incomplete", "paused"])
@pytest.mark.asyncio
async def test_non_entitled_status_never_grants(status) -> None:
    db = FakeDB([profile("u1", "free", "cus_1")])
    report = await reconcile_billing(db, client=FakeStripe([[sub("cus_1", STARTER, status)]]))
    assert db.writes == [], f"{status} granted a plan"
    assert report["healed"] == 0


@pytest.mark.asyncio
async def test_unmapped_price_never_grants() -> None:
    """Same refusal the webhook makes — never guess from an unknown price."""
    db = FakeDB([profile("u1", "free", "cus_1")])
    report = await reconcile_billing(db, client=FakeStripe([[sub("cus_1", "price_who_knows")]]))
    assert db.writes == []
    assert report["healed"] == 0


@pytest.mark.asyncio
async def test_an_unknown_stored_plan_is_not_blindly_upgraded() -> None:
    """`rank` returns 0 for an unrecognised plan, so a rank-only check would
    'upgrade' it to anything. The target must also be a known paid plan."""
    db = FakeDB([profile("u1", "enterprise_custom", "cus_1")])
    await reconcile_billing(db, client=FakeStripe([[sub("cus_1", STARTER)]]))
    # starter outranks unknown(0), so this DOES heal — that is intended and
    # safe (Stripe says they pay for starter). What must never happen is a
    # write to a plan Stripe did not name.
    assert db.writes == [("u1", {"plan": "starter"})]
    assert all(w[1]["plan"] in ("starter", "pro") for w in db.writes)


# --- gates ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_saas_makes_no_stripe_call(monkeypatch) -> None:
    """A self-hosted instance must not phone Stripe on a timer."""
    from app.config import settings

    monkeypatch.setattr(settings, "deployment_mode", "selfhost", raising=False)
    stripe = FakeStripe([[sub("cus_1", STARTER)]])
    db = FakeDB([profile("u1", "free", "cus_1")])
    report = await reconcile_billing(db, client=stripe)
    assert stripe.calls == [], "called Stripe outside saas mode"
    assert db.writes == []
    assert report["checked"] == 0


@pytest.mark.asyncio
async def test_missing_stripe_key_is_a_noop(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_secret_key", "", raising=False)
    stripe = FakeStripe([[sub("cus_1", STARTER)]])
    db = FakeDB([profile("u1", "free", "cus_1")])
    await reconcile_billing(db, client=stripe)
    assert stripe.calls == [] and db.writes == []


# --- failure isolation ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_stripe_outage_does_not_raise_or_write() -> None:
    """It runs from the scheduler; an escaping exception would be swallowed."""
    db = FakeDB([profile("u1", "free", "cus_1")])
    report = await reconcile_billing(db, client=FakeStripe([], explode=True))
    assert db.writes == []
    assert report["checked"] == 0


@pytest.mark.asyncio
async def test_a_db_read_failure_does_not_raise() -> None:
    db = FakeDB([profile("u1", "free", "cus_1")], read_fails=True)
    report = await reconcile_billing(db, client=FakeStripe([[sub("cus_1", STARTER)]]))
    assert report["healed"] == 0


@pytest.mark.asyncio
async def test_one_failed_write_does_not_abort_the_sweep() -> None:
    """A transient failure on one account must not strand the others."""
    db = FakeDB(
        [profile("u1", "free", "cus_1"), profile("u2", "free", "cus_2")],
        write_fails=True,
    )
    report = await reconcile_billing(
        db, client=FakeStripe([[sub("cus_1", STARTER), sub("cus_2", PRO, sid="sub_2")]])
    )
    assert report["healed"] == 0  # both writes failed, but it completed
    assert report["checked"] == 2


# --- pagination ----------------------------------------------------------


def test_pagination_walks_every_page() -> None:
    """One page would silently ignore everyone after the 100th subscription."""
    pages = [
        [sub(f"cus_{i}", STARTER, sid=f"sub_{i}") for i in range(100)],
        [sub(f"cus_{i}", STARTER, sid=f"sub_{i}") for i in range(100, 150)],
    ]
    stripe = FakeStripe(pages)
    got = _list_all_subscriptions(stripe)
    assert len(got) == 150, "pagination stopped early"
    assert len(stripe.calls) == 2
    assert stripe.calls[1]["starting_after"] == "sub_99"


def test_pagination_requests_status_all() -> None:
    """Filtering to active server-side would hide the lapsed accounts the
    report half exists to surface."""
    stripe = FakeStripe([[sub("cus_1", STARTER)]])
    _list_all_subscriptions(stripe)
    assert stripe.calls[0]["status"] == "all"


@pytest.mark.asyncio
async def test_subscriptions_on_later_pages_are_still_healed() -> None:
    """End to end through reconcile_billing, not just the pager."""
    pages = [
        [sub(f"cus_pad{i}", STARTER, sid=f"sub_pad{i}") for i in range(100)],
        [sub("cus_late", PRO, sid="sub_late")],
    ]
    db = FakeDB([profile("u_late", "free", "cus_late")])
    report = await reconcile_billing(db, client=FakeStripe(pages))
    assert db.writes == [("u_late", {"plan": "pro"})]
    assert report["healed"] == 1


# --- multiple subscriptions ---------------------------------------------


def test_multiple_entitled_plans_take_the_strongest() -> None:
    """Erring upward: they are paying for both, and granting the lesser would
    be taking something away."""
    got = _expected_plans(
        [_as_dict(x) for x in [sub("cus_1", STARTER, sid="a"), sub("cus_1", PRO, sid="b")]]
    )
    assert got == {"cus_1": "pro"}


def test_strongest_wins_regardless_of_order() -> None:
    """Choosing by list order would make the outcome depend on Stripe's paging."""
    got = _expected_plans(
        [_as_dict(x) for x in [sub("cus_1", PRO, sid="b"), sub("cus_1", STARTER, sid="a")]]
    )
    assert got == {"cus_1": "pro"}


def test_a_canceled_sub_does_not_mask_an_active_one() -> None:
    got = _expected_plans(
        [
            _as_dict(x)
            for x in [
                sub("cus_1", PRO, "canceled", sid="old"),
                sub("cus_1", STARTER, "active", sid="new"),
            ]
        ]
    )
    assert got == {"cus_1": "starter"}


# --- orphans -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_paying_customer_with_no_profile_is_reported() -> None:
    """A payment with no account attached is exactly what must not be silent."""
    db = FakeDB([])
    report = await reconcile_billing(db, client=FakeStripe([[sub("cus_ghost", PRO)]]))
    assert report["unknown_customer"] == 1
    assert db.writes == []


# --- the shared mapping --------------------------------------------------


def test_the_sweep_and_the_webhook_share_one_mapping() -> None:
    """Two copies would eventually disagree, and the sweep and the webhook
    would overwrite each other's writes forever."""
    from app.routers import billing
    from app.services import billing_plans

    assert billing._plan_for_price is billing_plans.plan_for_price
    assert billing._ENTITLED_STATUSES is billing_plans.ENTITLED_STATUSES


# --- the SDK returns objects, not dicts ----------------------------------


def test_sdk_objects_are_converted_before_use() -> None:
    """The bug real staging found: the SDK returns StripeObject, which has no
    `.get()`. The pager must normalise before anything reads fields."""
    stripe = FakeStripe([[sub("cus_1", STARTER)]])
    rows = _list_all_subscriptions(stripe)
    assert all(isinstance(r, dict) for r in rows), "SDK objects reached the caller"
    assert rows[0]["customer"] == "cus_1"


def test_nested_fields_survive_conversion() -> None:
    """A shallow to_dict() would leave StripeObjects underneath and move the
    crash one level down, into items.data[0].price.id."""
    rows = _list_all_subscriptions(FakeStripe([[sub("cus_1", PRO)]]))
    assert rows[0]["items"]["data"][0]["price"]["id"] == PRO


@pytest.mark.asyncio
async def test_end_to_end_with_sdk_style_objects() -> None:
    """The whole sweep, driven with non-dict subscriptions."""
    db = FakeDB([profile("u1", "free", "cus_1")])
    report = await reconcile_billing(db, client=FakeStripe([[sub("cus_1", PRO)]]))
    assert db.writes == [("u1", {"plan": "pro"})]
    assert report["healed"] == 1
