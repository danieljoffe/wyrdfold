"""Poller wiring for the #60 qualification firewall.

Covers ``poller._qualify_one_job`` / ``_qualify_jobs``:

- A new row (no ``qualified_hash``) is tagged: the LLM is called, cost is
  enqueued, and the full tag payload (+ ``qualified_at`` / ``qualified_hash``)
  is written back to the row.
- An unchanged row (``qualified_hash`` already matches its content + a prior
  ``qualified_at``) is skipped: no LLM call, no DB write — the content-hash
  cache makes a re-poll free.
- A changed row (content differs from the stored hash) is re-tagged.
- The step is best-effort: a tagger failure (tags=None) writes nothing and
  never raises; a write failure is swallowed.
- It bills the instance key (``get_llm_client(supabase, None)``), never a
  per-target payer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import settings as live_settings
from app.services import poller as poller_mod
from app.services.qualification import QualificationTags, qualification_hash

_TAGS = QualificationTags(
    is_us=True,
    us_confidence=98,
    role_family="engineering",
    seniority="senior_ic",
    employment_type="full_time",
    metro="San Francisco",
    is_remote=False,
    is_genuine_role=True,
)


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "job-1",
        "title": "Staff Engineer",
        "company_name": "Acme",
        "location": "San Francisco, CA",
        "description_html": "<p>Build things.</p>",
    }
    base.update(kw)
    return base


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tag_result: tuple[QualificationTags | None, object | None],
) -> dict[str, Any]:
    """Patch the poller's LLM client + tagger + cost-log + DB write, returning
    a recorder dict the tests assert against."""
    rec: dict[str, Any] = {
        "tag_calls": 0,
        "writes": [],
        "cost_calls": 0,
        "client_user_id": "UNSET",
    }

    def fake_get_client(supabase: object, user_id: str | None) -> object:
        rec["client_user_id"] = user_id
        return MagicMock(name="instance-client")

    async def fake_tag_job(_llm: object, **kwargs: Any) -> Any:
        rec["tag_calls"] += 1
        rec["last_tag_kwargs"] = kwargs
        return tag_result

    def fake_enqueue(user_id: str | None, purpose: str, result: object) -> None:
        rec["cost_calls"] += 1
        rec["cost_user_id"] = user_id
        rec["cost_purpose"] = purpose

    def fake_execute_with_retry_sync(fn: Any, *, label: str = "") -> Any:
        # The poller passes ``supabase.table(...).update(payload).eq(...).execute``
        # — a bound MagicMock method. We don't need to run it; record that a
        # write was attempted. The payload is captured separately below.
        return MagicMock(data=[])

    monkeypatch.setattr(poller_mod, "get_llm_client", fake_get_client)
    monkeypatch.setattr(poller_mod, "tag_job", fake_tag_job)
    monkeypatch.setattr(poller_mod, "enqueue_llm_cost", fake_enqueue)
    monkeypatch.setattr(poller_mod, "execute_with_retry_sync", fake_execute_with_retry_sync)
    return rec


def _supabase_capturing_updates(rec: dict[str, Any]) -> MagicMock:
    """A supabase mock whose ``.table('jobs').update(payload)`` records the
    payload into ``rec['writes']``."""
    sb = MagicMock()

    def update(payload: dict[str, Any]) -> MagicMock:
        rec["writes"].append(payload)
        chain = MagicMock()
        chain.eq.return_value.execute = MagicMock()
        return chain

    sb.table.return_value.update.side_effect = update
    return sb


class TestQualifyOneJob:
    @pytest.mark.asyncio
    async def test_new_row_is_tagged_and_written(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row()])

        assert rec["tag_calls"] == 1
        assert rec["cost_calls"] == 1
        assert rec["cost_user_id"] is None  # instance key, not a payer
        assert rec["cost_purpose"] == "qualification.tagger"
        assert len(rec["writes"]) == 1
        payload = rec["writes"][0]
        # Every tag column maps through.
        assert payload["is_us"] is True
        assert payload["role_family"] == "engineering"
        assert payload["seniority"] == "senior_ic"
        assert payload["employment_type"] == "full_time"
        assert payload["metro"] == "San Francisco"
        assert payload["is_remote"] is False
        assert payload["is_genuine_role"] is True
        assert payload["us_confidence"] == 98
        assert payload["qualified_at"] is not None
        # The persisted hash matches the row's content hash.
        assert payload["qualified_hash"] == qualification_hash(
            title="Staff Engineer",
            company="Acme",
            location="San Francisco, CA",
            description="<p>Build things.</p>",
        )

    @pytest.mark.asyncio
    async def test_unchanged_row_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        existing_hash = qualification_hash(
            title="Staff Engineer",
            company="Acme",
            location="San Francisco, CA",
            description="<p>Build things.</p>",
        )
        row = _row(qualified_hash=existing_hash, qualified_at="2026-06-24T00:00:00Z")

        await poller_mod._qualify_jobs(sb, [row])

        # Cache hit: no LLM call, no cost, no write.
        assert rec["tag_calls"] == 0
        assert rec["cost_calls"] == 0
        assert rec["writes"] == []

    @pytest.mark.asyncio
    async def test_changed_content_is_retagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        # Stored hash is for the OLD description; the row's current content
        # differs, so the cache must miss and re-tag.
        stale_hash = qualification_hash(
            title="Staff Engineer",
            company="Acme",
            location="San Francisco, CA",
            description="<p>OLD body.</p>",
        )
        row = _row(qualified_hash=stale_hash, qualified_at="2026-06-24T00:00:00Z")

        await poller_mod._qualify_jobs(sb, [row])

        assert rec["tag_calls"] == 1
        assert len(rec["writes"]) == 1

    @pytest.mark.asyncio
    async def test_tagger_failure_writes_nothing_and_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # tag_job fails soft → (None, None). The poller must not write or raise.
        rec = _patch_common(monkeypatch, tag_result=(None, None))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row()])

        assert rec["tag_calls"] == 1
        assert rec["cost_calls"] == 0
        assert rec["writes"] == []

    @pytest.mark.asyncio
    async def test_client_resolution_failure_skips_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_sb: object, _uid: str | None) -> object:
            raise RuntimeError("no client")

        monkeypatch.setattr(poller_mod, "get_llm_client", boom)
        # tag_job should never be reached.
        called = {"n": 0}

        async def fake_tag_job(*_a: object, **_k: object) -> Any:
            called["n"] += 1
            return None, None

        monkeypatch.setattr(poller_mod, "tag_job", fake_tag_job)

        # Must not raise even though the client can't be resolved.
        await poller_mod._qualify_jobs(MagicMock(), [_row()])
        assert called["n"] == 0


def _unique_rows(n: int) -> list[dict[str, Any]]:
    """``n`` rows with distinct ids/content so each would (absent the gate)
    miss the content-hash cache and trigger one ``tag_job`` call."""
    return [_row(id=f"job-{i}", title=f"Engineer {i}") for i in range(n)]


def _non_us_tags(confidence: int | None) -> QualificationTags:
    """A non-US verdict at the given ``us_confidence``."""
    return QualificationTags(
        is_us=False,
        us_confidence=confidence,
        role_family="engineering",
        seniority="senior_ic",
        employment_type="full_time",
        metro=None,
        is_remote=False,
        is_genuine_role=True,
    )


class TestNonUsArchive:
    """#60 workstream B: a high-confidence non-US verdict archives the job in
    the SAME tag write (``archived_at`` stamped), so non-US postings leave the
    live catalog a US-only product never surfaces. Conf-gated + reversible +
    OFF by default (a global-catalog self-host is unaffected)."""

    @pytest.mark.asyncio
    async def test_high_conf_non_us_is_archived_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live_settings, "qualification_archive_non_us", True)
        monkeypatch.setattr(live_settings, "qualification_non_us_archive_min_confidence", 80)
        rec = _patch_common(monkeypatch, tag_result=(_non_us_tags(95), object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row(location="London, UK")])

        assert len(rec["writes"]) == 1
        payload = rec["writes"][0]
        # Tagged non-US AND archived in the same write.
        assert payload["is_us"] is False
        assert payload["archived_at"] is not None

    @pytest.mark.asyncio
    async def test_non_us_not_archived_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clone-safe default: with the flag OFF, a non-US job is still tagged
        but NOT archived — the tagger records is_us for whoever wants it."""
        monkeypatch.setattr(live_settings, "qualification_archive_non_us", False)
        rec = _patch_common(monkeypatch, tag_result=(_non_us_tags(95), object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row(location="London, UK")])

        payload = rec["writes"][0]
        assert payload["is_us"] is False
        assert "archived_at" not in payload

    @pytest.mark.asyncio
    async def test_us_job_is_not_archived(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(live_settings, "qualification_archive_non_us", True)
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))  # is_us=True
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row()])

        assert "archived_at" not in rec["writes"][0]

    @pytest.mark.asyncio
    async def test_low_confidence_non_us_is_not_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Below the confidence floor the tagger's uncertain non-US call is
        left live — that band carries most US false-negatives."""
        monkeypatch.setattr(live_settings, "qualification_archive_non_us", True)
        monkeypatch.setattr(live_settings, "qualification_non_us_archive_min_confidence", 80)
        rec = _patch_common(monkeypatch, tag_result=(_non_us_tags(50), object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row(location="Remote")])

        payload = rec["writes"][0]
        assert payload["is_us"] is False
        assert "archived_at" not in payload

    @pytest.mark.asyncio
    async def test_none_confidence_non_us_is_not_archived_and_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the tolerant-tagger change (#60/#193) us_confidence can be None
        (a degraded malformed field). The archive comparison must guard against
        None — the job is simply left unarchived, not crashed."""
        monkeypatch.setattr(live_settings, "qualification_archive_non_us", True)
        monkeypatch.setattr(live_settings, "qualification_non_us_archive_min_confidence", 80)
        rec = _patch_common(monkeypatch, tag_result=(_non_us_tags(None), object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row(location="London, UK")])

        payload = rec["writes"][0]
        assert payload["is_us"] is False
        assert payload["us_confidence"] is None
        assert "archived_at" not in payload  # None confidence → not archived, no crash

    @pytest.mark.asyncio
    async def test_positively_us_location_vetoes_archive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Safety veto: even a high-confidence non-US verdict must NOT archive a
        job whose location plainly says US — the tagger false-negative hedge.
        (A real 'New York, NY, United States' was seen tagged non-US at 95.)"""
        monkeypatch.setattr(live_settings, "qualification_archive_non_us", True)
        monkeypatch.setattr(live_settings, "qualification_non_us_archive_min_confidence", 80)
        rec = _patch_common(monkeypatch, tag_result=(_non_us_tags(95), object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row(location="New York, NY, United States")])

        payload = rec["writes"][0]
        assert payload["is_us"] is False  # the (wrong) tag is still recorded
        assert "archived_at" not in payload  # but the job is NOT archived

    @pytest.mark.asyncio
    async def test_confidence_threshold_is_inclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """confidence == threshold archives (>=)."""
        monkeypatch.setattr(live_settings, "qualification_archive_non_us", True)
        monkeypatch.setattr(live_settings, "qualification_non_us_archive_min_confidence", 80)
        rec = _patch_common(monkeypatch, tag_result=(_non_us_tags(80), object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, [_row(location="Toronto, Canada")])

        assert rec["writes"][0]["archived_at"] is not None


class TestQualifyBudgetGate:
    """Fix 1: the qualification tagger bills the instance key and is invisible
    to the per-payer ``PayerBudgetGate``. Once today's GLOBAL LLM spend reaches
    ``global_llm_daily_budget_usd`` the tagger must STOP issuing LLM calls. The
    re-check runs between chunks, so a backlog can't grind past the cap.

    These are the regression that would have prevented the June overspend:
    delete the gate in ``_qualify_jobs`` and ``test_*over_budget*`` fail."""

    @pytest.mark.asyncio
    async def test_stops_when_over_budget_via_real_meter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end wiring: drive the gate through the REAL
        ``_global_budget_exhausted`` (settings cap + ``total_llm_spend_all``),
        not a stubbed predicate. Spend over cap → zero tagger calls."""
        monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
        # Today's spend already over the cap.
        monkeypatch.setattr(poller_mod, "total_llm_spend_all", MagicMock(return_value=12.5))
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        # A full chunk-plus of unique rows: absent the gate every one would
        # be tagged. With the gate, NONE are.
        rows = _unique_rows(poller_mod.QUALIFICATION_BUDGET_RECHECK_EVERY + 5)
        await poller_mod._qualify_jobs(sb, rows)

        assert rec["tag_calls"] == 0
        assert rec["cost_calls"] == 0
        assert rec["writes"] == []

    @pytest.mark.asyncio
    async def test_runs_normally_when_under_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate must not break the happy path: under the cap, every
        cache-missing row is tagged."""
        monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
        monkeypatch.setattr(poller_mod, "total_llm_spend_all", MagicMock(return_value=1.0))
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        rows = _unique_rows(3)
        await poller_mod._qualify_jobs(sb, rows)

        assert rec["tag_calls"] == 3
        assert len(rec["writes"]) == 3

    @pytest.mark.asyncio
    async def test_disabled_cap_never_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cap=0 disables the breaker; even a huge spend reading tags
        everything (operator opt-out)."""
        monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 0.0)
        spend = MagicMock(return_value=999.0)
        monkeypatch.setattr(poller_mod, "total_llm_spend_all", spend)
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, _unique_rows(2))

        assert rec["tag_calls"] == 2
        # cap<=0 short-circuits before the meter is read.
        spend.assert_not_called()

    @pytest.mark.asyncio
    async def test_overshoot_bounded_to_one_chunk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The cap is crossed AFTER the first chunk: chunk 1 tags, the
        re-check before chunk 2 sees 'exhausted' and defers the rest. Proves
        the bound is one chunk, not the whole backlog."""
        # Gate: under budget for the first check, over for the second.
        calls = {"n": 0}

        def fake_exhausted(_sb: object, *, reserve_usd: float = 0.0) -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # first chunk allowed, then deferred

        monkeypatch.setattr(poller_mod, "_global_budget_exhausted", fake_exhausted)
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        chunk = poller_mod.QUALIFICATION_BUDGET_RECHECK_EVERY
        rows = _unique_rows(chunk * 3)  # three chunks' worth
        await poller_mod._qualify_jobs(sb, rows)

        # Exactly one chunk got tagged before the gate tripped.
        assert rec["tag_calls"] == chunk
        assert calls["n"] == 2  # checked before chunk 1 (pass) and chunk 2 (trip)

    @pytest.mark.asyncio
    async def test_meter_read_failure_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the spend meter can't be read we refuse to spend (fail closed),
        matching the cycle gate's posture — no tagger calls, no raise."""
        monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
        monkeypatch.setattr(
            poller_mod,
            "total_llm_spend_all",
            MagicMock(side_effect=RuntimeError("db down")),
        )
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        # Must not raise.
        await poller_mod._qualify_jobs(sb, _unique_rows(3))

        assert rec["tag_calls"] == 0
        assert rec["writes"] == []


class TestGradingReserve:
    """The grading reserve fences off budget the background tagger can't touch,
    so live grading (Phase-1 triage + Phase-2 fit) is never starved by the
    tagger — the recurring drain that left new jobs stuck at ``stage2`` (#60).
    The tagger stops at ``cap - reserve`` while grading reads the full cap."""

    def test_reserve_lowers_the_taggers_effective_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spend sits in the reserved slice (between cap-reserve and cap):
        exhausted for the tagger (it yields), NOT for grading (full cap)."""
        monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
        monkeypatch.setattr(poller_mod, "total_llm_spend_all", MagicMock(return_value=8.0))
        sb = MagicMock()
        # $8 spent of $10: the tagger's $3 reserve makes it done ($8 >= $7)...
        assert poller_mod._global_budget_exhausted(sb, reserve_usd=3.0) is True
        # ...but grading (no reserve) still has room ($8 < $10).
        assert poller_mod._global_budget_exhausted(sb) is False

    def test_reserve_at_or_above_cap_makes_tagger_yield_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """reserve >= cap → effective cap 0 → the tagger always yields, without
        even reading the meter (a valid grading-only config)."""
        monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
        spend = MagicMock(return_value=0.0)
        monkeypatch.setattr(poller_mod, "total_llm_spend_all", spend)
        assert poller_mod._global_budget_exhausted(MagicMock(), reserve_usd=10.0) is True
        spend.assert_not_called()

    def test_disabled_cap_ignores_reserve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cap<=0 disables the breaker entirely — reserve is moot, meter unread."""
        monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 0.0)
        spend = MagicMock(return_value=999.0)
        monkeypatch.setattr(poller_mod, "total_llm_spend_all", spend)
        assert poller_mod._global_budget_exhausted(MagicMock(), reserve_usd=5.0) is False
        spend.assert_not_called()

    @pytest.mark.asyncio
    async def test_qualify_jobs_defers_in_the_reserve_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: with spend in the reserved slice, the tagger defers even
        though the full budget isn't spent — leaving the reserve for grading.
        (With reserve=0 the same spend would let the tagger run.)"""
        monkeypatch.setattr(live_settings, "global_llm_daily_budget_usd", 10.0)
        monkeypatch.setattr(live_settings, "grading_budget_reserve_usd", 3.0)
        monkeypatch.setattr(poller_mod, "total_llm_spend_all", MagicMock(return_value=8.0))
        rec = _patch_common(monkeypatch, tag_result=(_TAGS, object()))
        sb = _supabase_capturing_updates(rec)

        await poller_mod._qualify_jobs(sb, _unique_rows(3))

        assert rec["tag_calls"] == 0  # tagger yields the reserved slice
        assert rec["writes"] == []
