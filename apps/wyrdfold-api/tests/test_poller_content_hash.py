"""#642 — per-cycle change-detection (jobs.content_hash).

pg_stat evidence behind this: ~3.16M jobs updates over ~50k inserts (~63
rewrites per row, TOAST included) and ~7.3M scores updates (~35x), because
every cycle re-upserted every KNOWN row and re-ran stage-2 on it. Unchanged
rows must now skip the write entirely — and, because the scoring stages
iterate the upsert RESULT, skip the redundant rescore too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.targets import (
    CategoryProfile,
    JobTarget,
    ScoringProfile,
    SeniorityProfile,
)
from app.services.poller import _content_hash, _partition_unchanged
from app.services.standard_job import StandardJob

pytestmark = pytest.mark.asyncio


# ---- unit: hash + partition -------------------------------------------------


def _row(**overrides: object) -> dict:
    base: dict = {
        "external_id": "e-1",
        "source_id": "src-1",
        "title": "Senior Engineer",
        "company_name": "Acme",
        "location": "Remote, US",
        "description_html": "<p>Build things.</p>",
        "absolute_url": "https://example.com/j/1",
        "source_posted_at": "2026-01-01T00:00:00+00:00",
        "salary_text": "$150k",
    }
    base.update(overrides)
    return base


def test_content_hash_stable_and_field_sensitive() -> None:
    assert _content_hash(_row()) == _content_hash(_row())
    for field, changed in [
        ("title", "Staff Engineer"),
        ("location", "NYC"),
        ("description_html", "<p>Changed.</p>"),
        ("absolute_url", "https://example.com/j/2"),
        ("source_posted_at", "2026-02-01T00:00:00+00:00"),
        ("salary_text", "$180k"),
    ]:
        assert _content_hash(_row(**{field: changed})) != _content_hash(_row()), field
    # None and "" must not collide with each other across fields (separator).
    assert _content_hash(_row(salary_text=None)) != _content_hash(_row(salary_text="None"))


def test_partition_skips_only_known_unchanged() -> None:
    unchanged = _row(external_id="known-same")
    changed = _row(external_id="known-diff", title="Renamed Role")
    legacy = _row(external_id="known-null")
    fresh = _row(external_id="brand-new")
    stored = {
        "known-same": _content_hash(unchanged),
        "known-diff": _content_hash(_row(external_id="known-diff")),  # pre-rename
        "known-null": None,
    }

    to_write, skipped = _partition_unchanged([unchanged, changed, legacy, fresh], stored)

    assert skipped == 1
    ids = [r["external_id"] for r in to_write]
    assert ids == ["known-diff", "known-null", "brand-new"]
    # Every written row carries its stamp; the digest reflects current content.
    for r in to_write:
        assert r["content_hash"] == _content_hash(r)


# ---- end-to-end: unchanged known row skips the jobs upsert ------------------


def _admitting_target() -> JobTarget:
    from datetime import UTC, datetime

    return JobTarget(
        id="t-1",
        label="Engineer",
        scoring_profile=ScoringProfile(
            categories={"core": CategoryProfile(keywords={"engineer": 3}, weight=2.0)},
            seniority=SeniorityProfile(signals=[]),
        ),
        search_keywords=["senior engineer"],
        app_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _fetch_one_known() -> AsyncMock:
    async def fetch(_token: str) -> list[StandardJob]:
        return [
            StandardJob(
                external_id="known-1",
                title="Senior Engineer",
                location_name="Remote, US",
                content="<p>Build things.</p>",
                posted_at="2026-01-01",
                absolute_url="https://example.com/j/1",
            )
        ]

    return fetch  # type: ignore[return-value]


async def _run_cycle(monkeypatch, stored_hash: str | None) -> tuple[dict, MagicMock]:
    from app.services import poller as poller_mod
    from tests.test_poller import _GUARD_SOURCE, _make_poll_supabase

    existing = [
        {
            "id": "job-1",
            "external_id": "known-1",
            "title": "Senior Engineer",
            "company_name": "Acme",
        }
    ]
    supabase, jobs_table, _sources = _make_poll_supabase(existing)
    # Known-ids read now carries content_hash (#642).
    jobs_table.select.return_value.eq.return_value.execute.return_value.data = [
        {"external_id": "known-1", "content_hash": stored_hash}
    ]

    monkeypatch.setitem(poller_mod.FETCHERS, "greenhouse", _fetch_one_known())
    monkeypatch.setattr(
        poller_mod, "_active_targets", AsyncMock(return_value=[_admitting_target()])
    )
    monkeypatch.setattr(
        poller_mod, "_resolve_user_targets_for_stage3", AsyncMock(return_value=({}, {}))
    )
    open_gate = MagicMock()
    open_gate.target_blocked.return_value = False
    summary = await poller_mod._poll_one_source(
        dict(_GUARD_SOURCE), supabase, budget_gate=open_gate
    )
    return summary, jobs_table


async def test_unchanged_known_row_skips_jobs_upsert(monkeypatch) -> None:
    # First run against a NULL stored hash: the row writes once and the
    # payload carries the freshly computed stamp — capture it.
    summary, jobs_table = await _run_cycle(monkeypatch, stored_hash=None)
    assert jobs_table.upsert.called, "legacy NULL-hash row must write once"
    written = jobs_table.upsert.call_args[0][0]
    stamp = written[0]["content_hash"]
    assert stamp

    # Second run with that stamp stored: byte-identical content → NO jobs
    # upsert at all, and the skip is surfaced in the summary.
    summary2, jobs_table2 = await _run_cycle(monkeypatch, stored_hash=stamp)
    assert not jobs_table2.upsert.called, "unchanged known row must not rewrite"
    assert summary2.get("unchanged") == 1
    assert summary2["updated"] == 0
    assert summary2["error"] is None


async def test_changed_known_row_still_writes(monkeypatch) -> None:
    summary, jobs_table = await _run_cycle(monkeypatch, stored_hash="stale-different-hash")
    assert jobs_table.upsert.called
    written = jobs_table.upsert.call_args[0][0]
    assert written[0]["external_id"] == "known-1"
    assert written[0]["content_hash"] != "stale-different-hash"
    assert summary.get("unchanged") is None or summary.get("unchanged") == 0


# ---- spend memoize (#642) ---------------------------------------------------


async def test_total_spend_memoized_within_ttl(monkeypatch) -> None:
    """The mid-loop budget re-checks used to re-aggregate today's llm_costs
    on every call (236k calls in the pg_stat ledger). Within the TTL the
    meter is served from memory; expiry or a UTC-day rollover refreshes."""
    from datetime import UTC, datetime

    from app.services import poller as poller_mod

    calls = {"n": 0}

    async def fake_spend(_sb, *, since):
        calls["n"] += 1
        return 1.23

    monkeypatch.setattr(poller_mod, "total_llm_spend_all_async", fake_spend)
    # Reset the memo and pin the clock.
    poller_mod._spend_memo.update(at=0.0, midnight=None, value=0.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(poller_mod.time, "monotonic", lambda: clock["t"])

    midnight = datetime(2026, 8, 7, tzinfo=UTC)
    sb = MagicMock()
    assert await poller_mod._memoized_total_spend(sb, midnight) == 1.23
    assert await poller_mod._memoized_total_spend(sb, midnight) == 1.23
    assert calls["n"] == 1, "second call within TTL must hit the memo"

    clock["t"] += poller_mod._SPEND_MEMO_TTL_S + 1
    await poller_mod._memoized_total_spend(sb, midnight)
    assert calls["n"] == 2, "expired TTL must refresh"

    # Day rollover invalidates even within TTL.
    await poller_mod._memoized_total_spend(sb, datetime(2026, 8, 8, tzinfo=UTC))
    assert calls["n"] == 3
