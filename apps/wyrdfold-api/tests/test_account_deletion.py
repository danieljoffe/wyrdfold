"""Account deletion / right-to-erasure (#29 P1).

Pins the erasure contract:

* every per-user table is deleted by ``user_id`` (+ ``notifications_sent``
  by the resolved ``user_profiles.id``);
* both storage buckets' ``<user_id>/`` objects are purged;
* the auth user is deleted **last** (after the data);
* the shared catalog (``jobs`` / ``targets`` / ``scores`` / ``sources``)
  is NEVER touched — the multi-tenant safety invariant;
* the route surfaces it behind JWT-only auth using the service-role
  client.

Uses an in-memory fake supabase (tables + storage + auth.admin) that
records an ordered op log, so ordering and "what got touched" are
assertable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import account_deletion
from app.services.account_deletion import _USER_ID_TABLES

_UID = "u1"
_PROFILE_ID = "profile-1"

# Shared catalog — deleting any of these for a user would corrupt other
# tenants' data. The test asserts none are ever deleted.
_SHARED_TABLES = frozenset({"jobs", "targets", "scores", "sources"})


# ---- in-memory fakes --------------------------------------------------


class _FakeTableQuery:
    def __init__(self, name: str, rows: list[dict[str, Any]], log: list) -> None:
        self.name = name
        self._rows = rows
        self._log = log
        self._op: str | None = None
        self._filters: list[tuple[str, Any]] = []
        self._in_filters: list[tuple[str, list[Any]]] = []
        self._payload: dict[str, Any] = {}

    def delete(self) -> _FakeTableQuery:
        self._op = "delete"
        return self

    def select(self, _cols: str) -> _FakeTableQuery:
        self._op = "select"
        return self

    def update(self, payload: dict[str, Any]) -> _FakeTableQuery:
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> _FakeTableQuery:
        self._filters.append((col, val))
        return self

    def in_(self, col: str, vals: list[Any]) -> _FakeTableQuery:
        self._in_filters.append((col, list(vals)))
        return self

    def limit(self, _n: int) -> _FakeTableQuery:
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(row.get(c) == v for c, v in self._filters) and all(
            row.get(c) in vals for c, vals in self._in_filters
        )

    async def execute(self) -> SimpleNamespace:
        matched = [r for r in self._rows if self._matches(r)]
        self._log.append((self._op, self.name, dict(self._filters)))
        if self._op == "delete":
            self._rows[:] = [r for r in self._rows if not self._matches(r)]
        elif self._op == "update":
            for row in matched:
                row.update(self._payload)
        return SimpleNamespace(data=matched)


class _FakeBucket:
    def __init__(self, name: str, objects: dict[str, dict[str, list[str]]], log: list):
        self.name = name
        self._objects = objects
        self._log = log
        self.removed: list[list[str]] = []

    async def list(self, prefix: str) -> list[dict[str, str]]:
        return [{"name": n} for n in self._objects.get(self.name, {}).get(prefix, [])]

    async def remove(self, paths: list[str]) -> list[dict[str, str]]:
        self._log.append(("storage_remove", self.name, paths))
        self.removed.append(list(paths))
        for p in paths:
            prefix, name = p.split("/", 1)
            files = self._objects.get(self.name, {}).get(prefix, [])
            if name in files:
                files.remove(name)
        return [{"name": p} for p in paths]


class _FakeStorage:
    def __init__(self, objects: dict[str, dict[str, list[str]]], log: list) -> None:
        self._objects = objects
        self._log = log
        self.buckets: dict[str, _FakeBucket] = {}

    def from_(self, name: str) -> _FakeBucket:
        b = self.buckets.get(name) or _FakeBucket(name, self._objects, self._log)
        self.buckets[name] = b
        return b


class _FakeAdmin:
    def __init__(self, log: list) -> None:
        self._log = log
        self.deleted: list[str] = []

    async def delete_user(self, user_id: str, should_soft_delete: bool = False) -> None:
        self._log.append(("auth_delete", user_id, {}))
        self.deleted.append(user_id)


class _FakeRpc:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=self._data)


class _FakeSupabase:
    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
        objects: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        self.tables = tables or {}
        self.log: list = []
        self.storage = _FakeStorage(objects or {}, self.log)
        self.auth = SimpleNamespace(admin=_FakeAdmin(self.log))
        # Per-RPC canned return; the real reap returns whether a row went.
        self.rpc_results: dict[str, Any] = {"reap_orphaned_target": True}

    def table(self, name: str) -> _FakeTableQuery:
        return _FakeTableQuery(name, self.tables.setdefault(name, []), self.log)

    def rpc(self, name: str, params: dict[str, Any]) -> _FakeRpc:
        """Erasure calls ``reap_orphaned_target`` (#667).

        The fake NEEDS this: ``_reap_orphaned_target`` is fail-soft, so a
        ``_FakeSupabase`` without ``rpc`` would raise AttributeError, get
        swallowed, and every erasure test would pass while the reap never ran —
        certifying a code path that does nothing.
        """
        self.log.append(("rpc", name, params))
        return _FakeRpc(self.rpc_results.get(name, False))


def _seeded() -> _FakeSupabase:
    tables: dict[str, list[dict[str, Any]]] = {
        "documents": [{"user_id": _UID}, {"user_id": _UID}],
        "user_targets": [{"user_id": _UID, "target_id": "t1"}],
        "job_feedback": [{"user_id": _UID}],
        "user_api_keys": [{"user_id": _UID, "provider": "openrouter"}],
        "contribution_votes": [{"user_id": _UID, "reference_jd_id": "rj1"}],
        # Shared collective content: anonymized (user link nulled), not deleted.
        "reference_jds": [
            {"id": "rj1", "user_id": _UID, "jd_text": "JD A"},
            {"id": "rj2", "user_id": "other", "jd_text": "someone else's JD"},
        ],
        # Carries stripe_customer_id because the deletion path READS it before
        # deleting this row (#889). Without it the cancel-before-delete tests
        # would pass by returning early, proving nothing.
        "user_profiles": [
            {"id": _PROFILE_ID, "user_id": _UID, "stripe_customer_id": "cus_seeded"}
        ],
        # Keyed by the auth uid since R3 §2 (#557) — an ordinary user_id table.
        "notifications_sent": [
            {"user_id": _UID},
            {"user_id": _UID},
        ],
        # Shared catalog rows that must survive erasure.
        "scores": [{"target_id": "t1", "job_posting_id": "j1"}],
        "jobs": [{"id": "j1", "status": "applied"}],
        "targets": [{"id": "t1"}],
    }
    objects = {
        "resume-uploads": {_UID: ["up-1.pdf", "up-2.docx"]},
        "tailored-resumes": {_UID: ["r-1.docx"]},
    }
    return _FakeSupabase(tables, objects)


# ---- service ----------------------------------------------------------


async def test_reference_jds_anonymized_and_votes_deleted() -> None:
    """Erasure deletes the user's anonymous votes, and nulls the user link on
    their shared reference-JD contributions (collective content) rather than
    deleting the rows — never touching another contributor's JDs (#29)."""
    sb = _seeded()
    report = await account_deletion.delete_account(sb, user_id=_UID)

    # Votes deleted; reference_jds anonymized via UPDATE, never DELETE.
    assert ("delete", "contribution_votes", {"user_id": _UID}) in sb.log
    assert ("update", "reference_jds", {"user_id": _UID}) in sb.log
    deleted_tables = {table for op, table, _ in sb.log if op == "delete"}
    assert "reference_jds" not in deleted_tables

    rows = {r["id"]: r for r in sb.tables["reference_jds"]}
    assert rows["rj1"]["user_id"] is None  # personal link removed
    assert rows["rj1"]["jd_text"] == "JD A"  # shared content kept
    assert rows["rj2"]["user_id"] == "other"  # other contributor untouched
    assert report["reference_jds_anonymized"] == 1


async def test_deletes_every_per_user_table_by_user_id() -> None:
    sb = _seeded()
    report = await account_deletion.delete_account(sb, user_id=_UID)

    deleted = {(table, frozenset(filt.items())) for op, table, filt in sb.log if op == "delete"}
    expected_filter = frozenset({"user_id": _UID}.items())
    for table in _USER_ID_TABLES:
        assert (table, expected_filter) in deleted, table
        assert table in report


async def test_notifications_erased_by_auth_uid_with_no_profile_lookup() -> None:
    """R3 §2 (#557): ``notifications_sent`` is keyed by the auth uid, so it
    erases in the ordinary ``_USER_ID_TABLES`` loop and the ``user_profiles.id``
    surrogate resolution is gone entirely.
    """
    sb = _seeded()
    report = await account_deletion.delete_account(sb, user_id=_UID)
    assert ("delete", "notifications_sent", {"user_id": _UID}) in sb.log
    # A count, not just a present key — proves rows actually matched the filter
    # rather than the delete being issued against a column nothing is keyed by.
    assert report["notifications_sent"] == 2
    assert sb.tables["notifications_sent"] == []
    # The surrogate lookup is gone: erasure no longer SELECTs user_profiles at
    # all (it only deletes it, in its own step).
    assert not [e for e in sb.log if e[0] == "select" and e[1] == "user_profiles"]


async def test_shared_catalog_is_never_deleted() -> None:
    """The multi-tenant safety invariant: erasing one user must not delete
    rows from the shared catalog."""
    sb = _seeded()
    await account_deletion.delete_account(sb, user_id=_UID)
    deleted_tables = {table for op, table, _ in sb.log if op == "delete"}
    assert deleted_tables.isdisjoint(_SHARED_TABLES)
    # And the shared rows are physically still present. The scores row is
    # SCRUBBED in place (its Phase-2 PII nulled — asserted separately in
    # test_scrubs_shared_score_pii_for_user_targets); what matters here is that
    # it was not deleted.
    assert len(sb.tables["scores"]) == 1
    assert sb.tables["scores"][0]["target_id"] == "t1"
    assert sb.tables["scores"][0]["job_posting_id"] == "j1"
    assert sb.tables["jobs"] == [{"id": "j1", "status": "applied"}]


async def test_scrubs_shared_score_pii_for_user_targets() -> None:
    """Erasure nulls the Phase-2 grader PII on shared ``scores`` rows for
    the user's targets (without deleting the rows), re-opens them to grade,
    and leaves scores for *other* tenants' targets untouched."""
    tables: dict[str, list[dict[str, Any]]] = {
        "user_targets": [{"user_id": _UID, "target_id": "t1"}],
        "user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}],
        "scores": [
            {
                "target_id": "t1",
                "job_posting_id": "j1",
                "score": 80,
                "fit_reasoning": "Your FightCamp work (Lighthouse +40)",
                "axis_scores": {"skills_fit": 90},
                "logistics_filters": {"remote": True},
                "scoring_status": "complete",
            },
            # Another tenant's target — must NOT be touched.
            {
                "target_id": "t2",
                "job_posting_id": "j1",
                "fit_reasoning": "Someone else's resume",
                "scoring_status": "complete",
            },
        ],
    }
    sb = _FakeSupabase(tables, {})
    report = await account_deletion.delete_account(sb, user_id=_UID)

    scrubbed = next(r for r in sb.tables["scores"] if r["target_id"] == "t1")
    assert scrubbed["fit_reasoning"] is None
    assert scrubbed["axis_scores"] is None
    assert scrubbed["logistics_filters"] is None
    assert scrubbed["scoring_status"] == "stage2"
    assert scrubbed["score"] == 80  # numeric score left to re-grade
    assert report["scores_scrubbed"] == 1

    other = next(r for r in sb.tables["scores"] if r["target_id"] == "t2")
    assert other["fit_reasoning"] == "Someone else's resume"
    # The row was scrubbed, never deleted — still present.
    assert {r["target_id"] for r in sb.tables["scores"]} == {"t1", "t2"}
    assert "scores" not in {table for op, table, _ in sb.log if op == "delete"}


async def test_erasure_reaps_the_users_targets() -> None:
    """#667: erasure deletes ``user_targets`` by a different route than the
    unlink endpoint, and used to leave the shared row behind unreachable.

    Every id the user was linked to is offered to the guarded RPC; the guard
    (server-side, same snapshot as the delete) decides which actually go, so
    offering a co-followed target is a no-op rather than a cross-tenant delete.
    """
    sb = _seeded()

    report = await account_deletion.delete_account(sb, user_id=_UID)

    reaps = [entry for entry in sb.log if entry[0] == "rpc" and entry[1] == "reap_orphaned_target"]
    assert [r[2]["p_target_id"] for r in reaps] == ["t1"]
    assert report["targets_reaped"] == 1


async def test_erasure_reap_is_offered_every_linked_target() -> None:
    """The guard, not the caller, decides. Erasure must hand over every id —
    filtering client-side would need a membership count it no longer has."""
    sb = _FakeSupabase(
        {
            "user_targets": [{"user_id": _UID, "target_id": t} for t in ("t1", "t2", "t3")],
            "user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}],
        }
    )
    sb.rpc_results["reap_orphaned_target"] = False  # guard refuses them all

    report = await account_deletion.delete_account(sb, user_id=_UID)

    offered = [e[2]["p_target_id"] for e in sb.log if e[0] == "rpc"]
    assert offered == ["t1", "t2", "t3"]
    assert report["targets_reaped"] == 0


async def test_erasure_completes_when_the_reap_fails() -> None:
    """Fail-soft. The user's own data is already gone by step 3b; a tidy-up
    failure must not abort erasure and leave the account half-deleted."""

    class _Exploding(_FakeSupabase):
        def rpc(self, name: str, params: dict[str, Any]) -> Any:
            raise RuntimeError("rpc exploded")

    sb = _Exploding(
        {
            "user_targets": [{"user_id": _UID, "target_id": "t1"}],
            "user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}],
        }
    )

    report = await account_deletion.delete_account(sb, user_id=_UID)

    assert report["targets_reaped"] == 0
    # Erasure still ran to completion — the auth user is gone.
    assert any(entry[0] == "auth_delete" for entry in sb.log)


async def test_no_targets_skips_score_scrub() -> None:
    """A user with no target links issues no scores update (no ``.in_([])``).

    Uses a fake with genuinely zero ``user_targets`` rows. This used to lean on
    the seeded fixture's row having no ``target_id`` — a shape the database
    cannot produce (the column is NOT NULL), so the test was proving something
    about a row that never exists.
    """
    sb = _FakeSupabase(
        {
            "user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}],
            "scores": [{"target_id": "t1", "job_posting_id": "j1"}],
        }
    )
    report = await account_deletion.delete_account(sb, user_id=_UID)
    assert report["scores_scrubbed"] == 0
    assert ("update", "scores", {}) not in sb.log


async def test_both_storage_buckets_purged() -> None:
    sb = _seeded()
    report = await account_deletion.delete_account(sb, user_id=_UID)
    assert report["resume_uploads_objects"] == 2
    assert report["tailored_resume_objects"] == 1
    assert sb.storage.buckets["resume-uploads"].removed == [
        [f"{_UID}/up-1.pdf", f"{_UID}/up-2.docx"]
    ]
    assert sb.storage.buckets["tailored-resumes"].removed == [[f"{_UID}/r-1.docx"]]


async def test_auth_user_deleted_last() -> None:
    sb = _seeded()
    await account_deletion.delete_account(sb, user_id=_UID)
    ops = [(op, table) for op, table, _ in sb.log]
    assert ("auth_delete", _UID) in ops
    # auth deletion comes after the profile row delete (data first).
    assert ops.index(("auth_delete", _UID)) > ops.index(("delete", "user_profiles"))
    assert sb.auth.admin.deleted == [_UID]


async def test_erasure_covers_notifications_without_a_profile_row() -> None:
    """Erasure no longer depends on a ``user_profiles`` row existing.

    Before R3 §2 (#557) this case asserted the ledger was *skipped* — the
    profile surrogate could not be resolved, so the delete never ran. That was
    safe only because the FK made such rows impossible; the dependency is now
    gone, so the ledger is erased on its own terms.
    """
    sb = _FakeSupabase(
        tables={"documents": [{"user_id": _UID}], "notifications_sent": [{"user_id": _UID}]},
        objects={},
    )
    report = await account_deletion.delete_account(sb, user_id=_UID)
    assert report["notifications_sent"] == 1
    assert sb.tables["notifications_sent"] == []
    assert sb.auth.admin.deleted == [_UID]
    assert report["auth_user"] == 1


# ---- storage helper ---------------------------------------------------


async def test_purge_user_objects_empty_prefix_returns_zero() -> None:
    from app.services.ingest import storage

    sb = _FakeSupabase(objects={"resume-uploads": {}})
    assert await storage.purge_user_objects(sb, _UID) == 0


async def test_purge_user_objects_loops_until_empty() -> None:
    from app.services.tailor import persistence

    sb = _FakeSupabase(objects={"tailored-resumes": {_UID: ["a.docx", "b.docx"]}})
    assert await persistence.purge_user_objects(sb, _UID) == 2
    assert sb.storage.buckets["tailored-resumes"]._objects["tailored-resumes"][_UID] == []


# ---- route ------------------------------------------------------------


def test_delete_account_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import (
        get_async_service_supabase,
        get_current_user_id,
        verify_supabase_jwt,
    )
    from app.main import app

    sb = _seeded()
    app.dependency_overrides[verify_supabase_jwt] = lambda: _UID
    app.dependency_overrides[get_current_user_id] = lambda: _UID
    app.dependency_overrides[get_async_service_supabase] = lambda: sb
    try:
        client = TestClient(app)
        resp = client.delete("/profile/account")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["report"]["auth_user"] == 1
    assert body["report"]["documents"] == 2
    assert sb.auth.admin.deleted == [_UID]


# ---- deletion cancels the subscription (#889) --------------------------------
#
# Deleting an account used to leave the Stripe subscription billing — and the
# cascade removed ``user_profiles``, which held the only ``stripe_customer_id``,
# so afterwards nothing could connect the recurring charge back to a person.


def _delete_with_billing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subs: list[SimpleNamespace],
    cancel_raises: Exception | None = None,
) -> tuple[Any, Any, list[str]]:
    """Drive DELETE /profile/account with a faked Stripe, recording call order."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.dependencies import (
        get_async_service_supabase,
        get_current_user_id,
        verify_supabase_jwt,
    )
    from app.main import app
    from app.routers import billing

    monkeypatch.setattr(settings, "deployment_mode", "saas")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")

    order: list[str] = []
    cancelled: list[str] = []

    class _Subs:
        def list(self, params: dict[str, Any]) -> SimpleNamespace:
            order.append(f"stripe.list:{params.get('customer')}")
            return SimpleNamespace(data=subs)

        def cancel(self, sub_id: str) -> SimpleNamespace:
            if cancel_raises is not None:
                raise cancel_raises
            order.append(f"stripe.cancel:{sub_id}")
            cancelled.append(sub_id)
            return SimpleNamespace(id=sub_id, status="canceled")

    monkeypatch.setattr(billing, "_client", lambda: SimpleNamespace(subscriptions=_Subs()))

    sb = _seeded()

    # Mark where the erasure cascade BEGINS. Tracking raw table access would be
    # wrong: reading `stripe_customer_id` off user_profiles is itself a table
    # read, and it necessarily happens first. The property under test is that
    # cancellation finishes before any row is destroyed.
    real_cascade = account_deletion.delete_account

    async def _tracked_cascade(*args: Any, **kwargs: Any) -> Any:
        order.append("cascade:start")
        return await real_cascade(*args, **kwargs)

    monkeypatch.setattr(account_deletion, "delete_account", _tracked_cascade)

    app.dependency_overrides[verify_supabase_jwt] = lambda: _UID
    app.dependency_overrides[get_current_user_id] = lambda: _UID
    app.dependency_overrides[get_async_service_supabase] = lambda: sb
    try:
        resp = TestClient(app).delete("/profile/account")
    finally:
        app.dependency_overrides.clear()
    return resp, sb, order


def test_deletion_cancels_the_subscription_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering IS the bug (#889).

    ``stripe_customer_id`` lives on ``user_profiles``. Cancel after the cascade
    and there is no customer id left to cancel with — so this asserts the Stripe
    calls happen before ANY table write, not merely that both happened.
    """
    resp, _sb, order = _delete_with_billing(
        monkeypatch,
        subs=[SimpleNamespace(id="sub_live", status="active")],
    )

    assert resp.status_code == 200
    assert resp.json()["report"]["stripe_subscriptions_cancelled"] == 1

    assert order.index("stripe.cancel:sub_live") < order.index("cascade:start"), order


def test_deletion_is_refused_when_the_subscription_cannot_be_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail LOUD, and leave the account intact.

    The alternative — delete anyway — recreates the exact state #889 is about:
    a live subscription with nothing left to trace it to. A deletion the user
    can retry in a minute is the recoverable direction.
    """
    resp, sb, order = _delete_with_billing(
        monkeypatch,
        subs=[SimpleNamespace(id="sub_live", status="active")],
        cancel_raises=RuntimeError("stripe is down"),
    )

    assert resp.status_code == 503
    assert "haven’t deleted your account" in resp.json()["detail"].replace("'", "’")
    # The precondition that makes this meaningful: nothing was erased.
    assert sb.auth.admin.deleted == []
    assert "cascade:start" not in order
    assert sb.tables["documents"], "rows were destroyed despite the refusal"


@pytest.mark.parametrize(
    ("status", "expect_cancelled"),
    [
        ("active", True),
        ("trialing", True),
        # Delinquent subscriptions still bill once the card recovers, so they
        # must be cancelled too — listing only "active" would miss them.
        ("past_due", True),
        ("unpaid", True),
        ("paused", True),
        # Already terminal: cancelling again is a pointless API call.
        ("canceled", False),
        ("incomplete_expired", False),
    ],
)
def test_only_billable_subscription_statuses_are_cancelled(
    monkeypatch: pytest.MonkeyPatch, status: str, expect_cancelled: bool
) -> None:
    resp, _sb, order = _delete_with_billing(
        monkeypatch, subs=[SimpleNamespace(id="sub_x", status=status)]
    )

    assert resp.status_code == 200
    did_cancel = any(e.startswith("stripe.cancel") for e in order)
    assert did_cancel is expect_cancelled
    assert resp.json()["report"]["stripe_subscriptions_cancelled"] == int(expect_cancelled)


def test_deletion_without_billing_configured_never_calls_stripe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-host has no Stripe. Deletion must not depend on it."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.dependencies import (
        get_async_service_supabase,
        get_current_user_id,
        verify_supabase_jwt,
    )
    from app.main import app
    from app.routers import billing

    monkeypatch.setattr(settings, "deployment_mode", "self_host")
    monkeypatch.setattr(settings, "stripe_secret_key", "")

    def _boom() -> Any:  # pragma: no cover — must never be reached
        raise AssertionError("Stripe client built on a non-billing instance")

    monkeypatch.setattr(billing, "_client", _boom)

    sb = _seeded()
    app.dependency_overrides[verify_supabase_jwt] = lambda: _UID
    app.dependency_overrides[get_current_user_id] = lambda: _UID
    app.dependency_overrides[get_async_service_supabase] = lambda: sb
    try:
        resp = TestClient(app).delete("/profile/account")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["report"]["stripe_subscriptions_cancelled"] == 0
    assert sb.auth.admin.deleted == [_UID]
