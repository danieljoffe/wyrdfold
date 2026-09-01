"""Phase-1 backfill at target activation (#930).

The gap under test: ``bulk_title_score_for_target`` writes keyword-only
``stage1`` rows and never passes ``promising``, and the poller only ever
triages listings whose ``external_id`` is NEW — so a job admitted before a
target existed never receives a Phase-1 verdict, at activation or ever.

Every test here asserts its PRECONDITION before its outcome (the stored row
really is ``promising IS NULL`` before the pass; the old row really is in the
fake before the age bound drops it), because an assertion about what a pass
produced is vacuous unless the input it was supposed to act on provably
existed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.models.targets import (
    CategoryProfile,
    JobTarget,
    ScoringProfile,
    SeniorityProfile,
)
from app.services.llm.mock import MockLLMClient, phase1_triage_verdicts_json
from app.services.relevance import phase1_backfill as bf
from app.services.relevance.phase1_backfill import (
    Phase1BackfillResult,
    backfill_phase1_for_target,
)
from app.services.relevance.title_triage import PHASE1_PURPOSE
from tests.support.fake_backfill_db import backfill_supabase, cost_row

TARGET_ID = "t-1"


def _target(*, profile_version: int = 1) -> JobTarget:
    return JobTarget(
        id=TARGET_ID,
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(
            categories={"core_skills": CategoryProfile(keywords={"react": 3}, weight=2.0)},
            seniority=SeniorityProfile(signals=["staff"]),
        ),
        search_keywords=["frontend engineer"],
        app_active=True,
        profile_version=profile_version,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _job(job_id: str, title: str, *, days_ago: float, archived: bool = False) -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "cataloged_at": _iso(days_ago),
        "archived_at": _iso(0) if archived else None,
    }


def _score(job_id: str, *, promising: bool | None = None, excluded: bool = False) -> dict[str, Any]:
    return {
        "job_posting_id": job_id,
        "target_id": TARGET_ID,
        "score": 40,
        "excluded": excluded,
        "scoring_status": "stage1",
        "promising": promising,
    }


_NUMBERED = re.compile(r"^\s*(\d+)\.\s+(.*)$")


def _titles_from_prompt(latest_user: str) -> list[str]:
    """The batch as the model would see it, recovered from the rendered user
    message. Doubles as a check that the prompt really carries the titles in
    the order the caller passed them."""
    out: list[tuple[int, str]] = []
    for line in latest_user.splitlines():
        m = _NUMBERED.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2).strip()))
    return [t for _, t in sorted(out)]


def _llm(variant: str = "faithful") -> MockLLMClient:
    """A mock LLM that answers the real Phase-1 prompt with a real (or
    deliberately malformed) verdict payload from the mock's bug corpus."""
    client = MockLLMClient()
    client.register(
        PHASE1_PURPOSE,
        lambda latest_user, _messages: phase1_triage_verdicts_json(
            _titles_from_prompt(latest_user), variant
        ),
    )
    return client


def _batches(client: MockLLMClient) -> list[list[str]]:
    return [
        _titles_from_prompt(
            next(m.content for m in reversed(call["messages"]) if m.role == "user")  # type: ignore[union-attr,index]
        )
        for call in client.calls
    ]


@pytest.fixture(autouse=True)
def _enable_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phase1_triage_enabled", True)
    monkeypatch.setattr(settings, "phase1_backfill_enabled", True)
    monkeypatch.setattr(settings, "phase1_backfill_max_age_days", 30)
    monkeypatch.setattr(settings, "phase1_daily_cap", 100)
    monkeypatch.setattr(settings, "phase1_backfill_cap_fraction", 0.25)
    monkeypatch.setattr(settings, "phase1_rejection_ttl_hours", 1440.0)


# ---------------------------------------------------------------------------
# The gap itself: activation must produce REAL verdicts, not just stage1 rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_writes_real_promising_verdicts_over_stage1_rows() -> None:
    """The headline. ``bulk_title_score_for_target`` leaves ``promising`` NULL;
    this pass fills it in with an actual LLM verdict."""
    jobs = [
        _job("j1", "Frontend Engineer", days_ago=1),
        _job("j2", "Account Executive", days_ago=2),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1"), _score("j2")])

    # PRECONDITION: the stage1 rows exist and carry NO Phase-1 verdict, which
    # is exactly the state the activation fan-out leaves behind.
    assert [r["promising"] for r in supabase._scores.rows.values()] == [None, None]
    assert all(r["scoring_status"] == "stage1" for r in supabase._scores.rows.values())

    client = _llm()
    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert result.llm_calls == 1
    assert result.verdicts_written == 2
    stored = supabase._scores.rows
    # The corpus's ``faithful`` variant answers promising for odd ids — id 1
    # is the newest job (j1), id 2 the older one (j2).
    assert stored[("j1", TARGET_ID)]["promising"] is True
    assert stored[("j2", TARGET_ID)]["promising"] is False
    assert stored[("j1", TARGET_ID)]["phase1_confidence"] == 92
    # A Phase-1 rejection excludes the row — that is what "judged" means.
    assert stored[("j2", TARGET_ID)]["excluded"] is True
    assert stored[("j1", TARGET_ID)]["excluded"] is False
    # The keyword columns the fan-out wrote are untouched (merge, not replace).
    assert stored[("j1", TARGET_ID)]["score"] == 40


@pytest.mark.asyncio
async def test_backfill_leaves_already_graded_rows_alone() -> None:
    """Only ``promising IS NULL`` rows are candidates — a graded row is never
    re-billed."""
    jobs = [
        _job("j1", "Frontend Engineer", days_ago=1),
        _job("j2", "Staff Web Engineer", days_ago=2),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1", promising=True), _score("j2")])
    assert supabase._scores.rows[("j1", TARGET_ID)]["promising"] is True  # precondition

    client = _llm()
    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert result.candidates == 1
    assert _batches(client) == [["Staff Web Engineer"]]


@pytest.mark.asyncio
async def test_backfill_is_a_noop_when_the_flag_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phase1_backfill_enabled", False)
    supabase = backfill_supabase(
        jobs=[_job("j1", "Frontend Engineer", days_ago=1)], scores=[_score("j1")]
    )
    client = _llm()

    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert result.stopped == "disabled"
    assert client.calls == []
    assert supabase._scores.rows[("j1", TARGET_ID)]["promising"] is None


@pytest.mark.asyncio
async def test_backfill_defers_without_writing_when_phase1_is_unavailable() -> None:
    """A failed LLM call must NOT fail open into unjudged admits — the titles
    stay NULL and re-enter the next activation (#285/#294)."""
    jobs = [_job("j1", "Frontend Engineer", days_ago=1)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1")])
    client = MockLLMClient()
    # Prose instead of the forced tool call — the shape a provider error or a
    # refusal takes; ``triage_titles`` swallows it and returns no verdicts.
    client.register(PHASE1_PURPOSE, "I'm sorry, I can't help with that.")

    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert result.stopped == "llm_unavailable"
    assert result.verdicts_written == 0
    assert supabase._scores.rows[("j1", TARGET_ID)]["promising"] is None


# ---------------------------------------------------------------------------
# Ordering + the age bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_grades_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bf, "phase1_batch_size", lambda *_a, **_k: 2)
    jobs = [
        _job("old", "Old Engineer", days_ago=20),
        _job("new", "New Engineer", days_ago=1),
        _job("mid", "Mid Engineer", days_ago=10),
        _job("older", "Older Engineer", days_ago=25),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score(j["id"]) for j in jobs])
    client = _llm()

    await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert _batches(client) == [
        ["New Engineer", "Mid Engineer"],
        ["Old Engineer", "Older Engineer"],
    ]


@pytest.mark.asyncio
async def test_age_bound_excludes_rows_older_than_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "phase1_backfill_max_age_days", 30)
    jobs = [
        _job("fresh", "Fresh Engineer", days_ago=5),
        _job("stale", "Stale Engineer", days_ago=45),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("fresh"), _score("stale")])
    # PRECONDITION: the old row IS a candidate by every other criterion — it
    # has an ungraded score row for this target. Only its age excludes it.
    assert supabase._scores.rows[("stale", TARGET_ID)]["promising"] is None

    client = _llm()
    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert _batches(client) == [["Fresh Engineer"]]
    assert result.candidates == 1
    assert supabase._scores.rows[("stale", TARGET_ID)]["promising"] is None


@pytest.mark.asyncio
async def test_age_bound_widens_with_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control for the test above: the same 45-day row IS graded once the
    configured bound covers it, proving the exclusion came from the bound and
    not from some other filter."""
    monkeypatch.setattr(settings, "phase1_backfill_max_age_days", 60)
    jobs = [_job("stale", "Stale Engineer", days_ago=45)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("stale")])

    client = _llm()
    await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert _batches(client) == [["Stale Engineer"]]
    assert supabase._scores.rows[("stale", TARGET_ID)]["promising"] is True


@pytest.mark.asyncio
async def test_archived_rows_are_never_graded() -> None:
    jobs = [
        _job("live", "Live Engineer", days_ago=2),
        _job("gone", "Gone Engineer", days_ago=2, archived=True),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("live"), _score("gone")])
    assert supabase._scores.rows[("gone", TARGET_ID)]["promising"] is None  # precondition

    client = _llm()
    await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert _batches(client) == [["Live Engineer"]]


@pytest.mark.asyncio
async def test_backfill_pages_through_a_window_larger_than_one_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bf, "_JOBS_PAGE_SIZE", 2)
    monkeypatch.setattr(bf, "phase1_batch_size", lambda *_a, **_k: 10)
    jobs = [_job(f"j{i}", f"Engineer {i}", days_ago=i + 1) for i in range(5)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score(j["id"]) for j in jobs])
    client = _llm()

    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert result.candidates == 5
    # One LLM call per page (3 pages: 2 + 2 + 1), newest-first across pages.
    assert _batches(client) == [
        ["Engineer 0", "Engineer 1"],
        ["Engineer 2", "Engineer 3"],
        ["Engineer 4"],
    ]
    assert all(r["promising"] is not None for r in supabase._scores.rows.values())


# ---------------------------------------------------------------------------
# The shared daily cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_cap_halts_the_backfill_mid_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is SHARED: rows the poller's fresh ingestion already wrote today
    are what stops the backfill here, not a counter of its own."""
    monkeypatch.setattr(settings, "phase1_daily_cap", 4)
    monkeypatch.setattr(settings, "phase1_backfill_cap_fraction", 1.0)
    monkeypatch.setattr(bf, "phase1_batch_size", lambda *_a, **_k: 1)
    jobs = [_job(f"j{i}", f"Engineer {i}", days_ago=i + 1) for i in range(4)]
    # Three of the day's four calls already spent by ordinary ingestion.
    supabase = backfill_supabase(
        jobs=jobs,
        scores=[_score(j["id"]) for j in jobs],
        costs=[cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE) for _ in range(3)],
    )
    client = _llm()

    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    # One call available; after it the shared counter reads 4/4 and the pass
    # stops rather than grading the rest.
    assert result.allowance == 1
    assert result.llm_calls == 1
    assert result.stopped == "allowance"
    assert _batches(client) == [["Engineer 0"]]
    assert supabase._scores.rows[("j3", TARGET_ID)]["promising"] is None


@pytest.mark.asyncio
async def test_concurrent_ingestion_spend_stops_the_backfill_mid_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of "shared": the allowance is computed once, so the cap
    is ALSO re-read between batches. A poll cycle that spends the day's
    remaining calls while a backfill is running stops that backfill."""
    monkeypatch.setattr(settings, "phase1_daily_cap", 10)
    monkeypatch.setattr(settings, "phase1_backfill_cap_fraction", 1.0)
    monkeypatch.setattr(bf, "phase1_batch_size", lambda *_a, **_k: 1)
    jobs = [_job(f"j{i}", f"Engineer {i}", days_ago=i + 1) for i in range(4)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score(j["id"]) for j in jobs])

    client = MockLLMClient()

    def _respond(latest_user: str, _messages: object) -> str:
        # Fresh ingestion burns the rest of the day's cap while this call is
        # in flight.
        supabase._llm_costs.rows.extend(
            cost_row(target_id=TARGET_ID, purpose=PHASE1_PURPOSE) for _ in range(10)
        )
        return phase1_triage_verdicts_json(_titles_from_prompt(latest_user), "faithful")

    client.register(PHASE1_PURPOSE, _respond)

    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert result.allowance == 10  # not the binding constraint
    assert result.llm_calls == 1
    assert result.stopped == "daily_cap"


@pytest.mark.asyncio
async def test_backfill_cannot_consume_the_whole_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large activation must leave the day's remaining calls for fresh
    intake. With a cap of 10 and a 0.25 share the backfill gets 2, even though
    10 batches are waiting and nothing else has spent anything today."""
    monkeypatch.setattr(settings, "phase1_daily_cap", 10)
    monkeypatch.setattr(settings, "phase1_backfill_cap_fraction", 0.25)
    monkeypatch.setattr(bf, "phase1_batch_size", lambda *_a, **_k: 1)
    jobs = [_job(f"j{i}", f"Engineer {i}", days_ago=i + 1) for i in range(10)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score(j["id"]) for j in jobs])
    # PRECONDITION: the day is untouched, so only the FRACTION can be the bound.
    assert supabase._llm_costs.rows == []

    client = _llm()
    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert result.allowance == 2
    assert result.llm_calls == 2
    assert result.stopped == "allowance"
    # And the shared counter proves what is left for the rest of the day.
    assert len(supabase._llm_costs.rows) == 2
    assert settings.phase1_daily_cap - len(supabase._llm_costs.rows) == 8


@pytest.mark.asyncio
async def test_backfill_calls_count_against_the_shared_cap() -> None:
    """The mechanism behind the guarantee above: every backfill call writes the
    same ``purpose``/``target_id`` cost row the poller writes, so the two
    spenders read ONE counter."""
    jobs = [_job("j1", "Frontend Engineer", days_ago=1)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1")])

    await backfill_phase1_for_target(supabase, _llm(), _target(), payer_user_id="u-1")

    assert len(supabase._llm_costs.rows) == 1
    row = supabase._llm_costs.rows[0]
    assert row["purpose"] == PHASE1_PURPOSE
    assert row["metadata"]["target_id"] == TARGET_ID
    assert row["metadata"]["trigger"] == "activation_backfill"


@pytest.mark.asyncio
async def test_cap_of_zero_leaves_the_backfill_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "phase1_daily_cap", 0)
    monkeypatch.setattr(bf, "phase1_batch_size", lambda *_a, **_k: 1)
    jobs = [_job(f"j{i}", f"Engineer {i}", days_ago=i + 1) for i in range(3)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score(j["id"]) for j in jobs])

    result = await backfill_phase1_for_target(supabase, _llm(), _target(), payer_user_id="u-1")

    assert result.allowance is None
    assert result.llm_calls == 3
    assert result.stopped is None


@pytest.mark.asyncio
async def test_global_budget_stops_the_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bf, "phase1_batch_size", lambda *_a, **_k: 1)
    jobs = [_job(f"j{i}", f"Engineer {i}", days_ago=i + 1) for i in range(3)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score(j["id"]) for j in jobs])
    calls = {"n": 0}

    async def _blocks() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # first batch passes, then the meter trips

    client = _llm()
    result = await backfill_phase1_for_target(
        supabase, client, _target(), payer_user_id="u-1", budget_blocks=_blocks
    )

    assert result.llm_calls == 1
    assert result.stopped == "global_budget"


# ---------------------------------------------------------------------------
# The negative-verdict store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_hits_are_served_without_an_llm_call() -> None:
    """A title this target already rejected inside the TTL costs no LLM call —
    the single biggest reason a full backfill is cheap."""
    jobs = [
        _job("j1", "Frontend Engineer", days_ago=1),
        _job("j2", "Account  Executive", days_ago=2),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1"), _score("j2")])
    # Seed the store the way ``record_rejections`` would: normalized title.
    supabase._phase1_rejections.rows[(TARGET_ID, 1, "account executive")] = {
        "target_id": TARGET_ID,
        "profile_version": 1,
        "title_norm": "account executive",
        "confidence": 91,
        "model": "claude-haiku-4-5",
        "judged_at": datetime.now(UTC).isoformat(),
    }

    client = _llm()
    result = await backfill_phase1_for_target(supabase, client, _target(), payer_user_id="u-1")

    assert result.store_hits == 1
    # The stored title normalizes to the cached key (double space collapsed),
    # so it never reaches the model.
    assert _batches(client) == [["Frontend Engineer"]]
    assert supabase._scores.rows[("j2", TARGET_ID)]["promising"] is False
    assert supabase._scores.rows[("j2", TARGET_ID)]["excluded"] is True
    # A store hit is free: no cost row, so it does not eat the shared cap.
    assert len(supabase._llm_costs.rows) == 1


@pytest.mark.asyncio
async def test_store_miss_on_a_bumped_profile_version_re_pays() -> None:
    """Control for the test above: the SAME stored rejection stops applying
    once the profile version moves, so the skip really is keyed on the store
    and not on the title alone."""
    jobs = [_job("j2", "Account Executive", days_ago=2)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j2")])
    supabase._phase1_rejections.rows[(TARGET_ID, 1, "account executive")] = {
        "target_id": TARGET_ID,
        "profile_version": 1,
        "title_norm": "account executive",
        "confidence": 91,
        "model": "claude-haiku-4-5",
        "judged_at": datetime.now(UTC).isoformat(),
    }

    client = _llm()
    result = await backfill_phase1_for_target(
        supabase, client, _target(profile_version=2), payer_user_id="u-1"
    )

    assert result.store_hits == 0
    assert _batches(client) == [["Account Executive"]]


@pytest.mark.asyncio
async def test_fresh_rejections_are_persisted_for_the_next_pass() -> None:
    jobs = [
        _job("j1", "Frontend Engineer", days_ago=1),
        _job("j2", "Account Executive", days_ago=2),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1"), _score("j2")])
    assert supabase._phase1_rejections.rows == {}  # precondition

    await backfill_phase1_for_target(supabase, _llm(), _target(), payer_user_id="u-1")

    assert (TARGET_ID, 1, "account executive") in supabase._phase1_rejections.rows
    # Only the "no" is cached — a promising verdict is not a rejection.
    assert (TARGET_ID, 1, "frontend engineer") not in supabase._phase1_rejections.rows


# ---------------------------------------------------------------------------
# The LLM bug corpus: malformed verdict payloads must not corrupt `scores`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_verdicts_fail_open_to_promising() -> None:
    """The model omitting ids is routine. A missing verdict admits — a false
    negative here is lost forever."""
    jobs = [_job(f"j{i}", f"Engineer {i}", days_ago=i + 1) for i in range(4)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score(j["id"]) for j in jobs])

    result = await backfill_phase1_for_target(
        supabase, _llm("omits_ids"), _target(), payer_user_id="u-1"
    )

    assert result.verdicts_written == 4
    # Ids 3-4 got no verdict at all and still land promising.
    assert supabase._scores.rows[("j2", TARGET_ID)]["promising"] is True
    assert supabase._scores.rows[("j3", TARGET_ID)]["promising"] is True
    assert supabase._scores.rows[("j2", TARGET_ID)]["phase1_confidence"] is None


@pytest.mark.asyncio
async def test_transposed_prefixes_are_dropped_not_misassigned() -> None:
    """The ``title_prefix`` cross-check (#47): ids that look valid but echo
    another title's prefix must be discarded and fail open, never applied to
    the wrong listing."""
    jobs = [
        _job("j1", "Frontend Engineer", days_ago=1),
        _job("j2", "Account Executive", days_ago=2),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1"), _score("j2")])

    await backfill_phase1_for_target(
        supabase, _llm("transposed_prefix"), _target(), payer_user_id="u-1"
    )

    # Faithful grading would have made j2 (id 2, even) unpromising. Every
    # verdict was dropped, so both fail open instead of j2 inheriting j1's.
    assert supabase._scores.rows[("j1", TARGET_ID)]["promising"] is True
    assert supabase._scores.rows[("j2", TARGET_ID)]["promising"] is True
    assert supabase._phase1_rejections.rows == {}


@pytest.mark.asyncio
async def test_out_of_range_ids_do_not_shift_verdicts() -> None:
    jobs = [
        _job("j1", "Frontend Engineer", days_ago=1),
        _job("j2", "Account Executive", days_ago=2),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1"), _score("j2")])

    result = await backfill_phase1_for_target(
        supabase, _llm("out_of_range_ids"), _target(), payer_user_id="u-1"
    )

    assert result.verdicts_written == 2
    assert supabase._scores.rows[("j1", TARGET_ID)]["promising"] is True
    assert supabase._scores.rows[("j2", TARGET_ID)]["promising"] is False


@pytest.mark.asyncio
async def test_low_confidence_promising_is_not_admitted() -> None:
    """A hedged "promising" is gated out by ``phase1_min_confidence`` — a guess
    must not buy a Phase-2 grade."""
    jobs = [_job("j1", "Frontend Engineer", days_ago=1)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1")])

    await backfill_phase1_for_target(
        supabase, _llm("low_confidence"), _target(), payer_user_id="u-1"
    )

    stored = supabase._scores.rows[("j1", TARGET_ID)]
    assert stored["promising"] is False
    assert stored["phase1_confidence"] == 25
    # The RAW verdict was promising, so this is not a rejection to cache.
    assert supabase._phase1_rejections.rows == {}


@pytest.mark.asyncio
async def test_truncated_json_defers_the_batch() -> None:
    """The deepseek output-ceiling failure: a mid-JSON truncation must defer
    the batch, never parse into a partial admit set."""
    jobs = [_job("j1", "Frontend Engineer", days_ago=1)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1")])

    result = await backfill_phase1_for_target(
        supabase, _llm("truncated"), _target(), payer_user_id="u-1"
    )

    assert result.stopped == "llm_unavailable"
    assert supabase._scores.rows[("j1", TARGET_ID)]["promising"] is None


# ---------------------------------------------------------------------------
# Exclusion carry-over
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promising_verdict_does_not_un_exclude_a_keyword_rejection() -> None:
    jobs = [_job("j1", "Frontend Engineer", days_ago=1)]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1", excluded=True)])
    assert supabase._scores.rows[("j1", TARGET_ID)]["excluded"] is True  # precondition

    await backfill_phase1_for_target(supabase, _llm(), _target(), payer_user_id="u-1")

    stored = supabase._scores.rows[("j1", TARGET_ID)]
    assert stored["promising"] is True
    assert stored["excluded"] is True


@pytest.mark.asyncio
async def test_every_written_row_carries_the_same_key_set() -> None:
    """#928: a PostgREST bulk upsert writes the union of the batch's keys to
    every row, so a payload with a ragged key set would NULL a column on the
    rows that omitted it. Uniformity is the guard."""
    jobs = [
        _job("j1", "Frontend Engineer", days_ago=1),
        _job("j2", "Account Executive", days_ago=2),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[_score("j1"), _score("j2")])

    await backfill_phase1_for_target(supabase, _llm(), _target(), payer_user_id="u-1")

    for payload in supabase._scores.upsert_calls:
        assert len({frozenset(row) for row in payload}) == 1
        assert frozenset(payload[0]) == {
            "job_posting_id",
            "target_id",
            "promising",
            "phase1_confidence",
            "excluded",
            "updated_at",
        }


# ---------------------------------------------------------------------------
# The activation pipeline actually runs it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activation_pipeline_judges_what_the_retro_score_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End of the wire. ``_activate_pipeline`` step 3 surfaces pre-existing
    postings with keyword-only ``stage1`` rows; step 4 must judge them, or the
    user gets a list nothing ever looked at."""
    from app.routers import targets as targets_router

    monkeypatch.setattr(settings, "global_llm_daily_budget_usd", 0.0)

    jobs = [
        _job("j1", "Frontend Engineer", days_ago=1),
        _job("j2", "Account Executive", days_ago=2),
    ]
    supabase = backfill_supabase(jobs=jobs, scores=[])

    async def _fake_retro(_sb: Any, target: JobTarget, **_kw: Any) -> int:
        # Stand in for the real fan-out: keyword rows, ``promising`` untouched.
        for job in jobs:
            supabase._scores.rows[(job["id"], target.id)] = _score(job["id"])
        return len(jobs)

    statuses: list[str] = []

    async def _fake_update(_sb: Any, _tid: str, update: Any) -> JobTarget:
        statuses.append(update.activation_status)
        return _target()

    monkeypatch.setattr(targets_router, "bulk_title_score_for_target", _fake_retro)
    monkeypatch.setattr(targets_router, "poll_sources_for_target", AsyncMock())
    monkeypatch.setattr(targets_router, "_update_target_async", _fake_update)

    client = _llm()
    await targets_router._activate_pipeline(supabase, client, _target(), "u-1")

    assert statuses[-1] == "ready"  # the pipeline completed, not errored
    # PRECONDITION met by the fan-out stand-in, OUTCOME by the backfill.
    assert supabase._scores.rows[("j1", TARGET_ID)]["promising"] is True
    assert supabase._scores.rows[("j2", TARGET_ID)]["promising"] is False
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Resumability: an early stop must have an automatic path to completion
# ---------------------------------------------------------------------------


def _unblocked_gate(monkeypatch: pytest.MonkeyPatch, reason: str | None = None) -> MagicMock:
    """Budget gate stub for the resume sweep. ``reason=None`` = spend allowed."""
    from app.services import poller as poller_mod

    gate = MagicMock()
    gate.target_block_reason.return_value = reason
    monkeypatch.setattr(poller_mod, "build_budget_gate", AsyncMock(return_value=gate))
    return gate


def _unblocked_gate(
    monkeypatch: pytest.MonkeyPatch,
    reason: str | None = None,
    payer: str | None = "u-1",
) -> MagicMock:
    """Budget-gate stub for the resume sweep. ``reason=None`` = spend allowed.

    Stubs ``payer_for`` too, because the sweep takes the payer FROM the gate:
    one snapshot answers both "may we spend here" and "who pays"."""
    from app.services import poller as poller_mod

    gate = MagicMock()
    gate.target_block_reason.return_value = reason
    gate.payer_for.return_value = payer
    monkeypatch.setattr(poller_mod, "build_budget_gate", AsyncMock(return_value=gate))
    return gate


@pytest.mark.asyncio
async def test_a_capped_pass_is_resumed_without_a_user_toggling_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocker this sweep exists for.

    Stopping early is part of the design — own allowance, shared daily cap,
    global budget, provider outage. But the rows a stopped pass did not reach
    keep ``promising IS NULL``, and NOTHING else judges them: ordinary polling
    triages only externally-new listings. Activation was the sole caller, so an
    early stop stayed permanent unless a user toggled the target off and on.

    This proves the second run finishes the job with no user action: run one
    stops on its allowance, run two grades what run one left."""
    from app.services import poller as poller_mod

    target = _target()
    monkeypatch.setattr(poller_mod.settings, "phase1_backfill_enabled", True)
    monkeypatch.setattr(poller_mod, "_active_targets", AsyncMock(return_value=[target]))
    monkeypatch.setattr(poller_mod, "_resolve_payer_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(poller_mod, "_global_budget_exhausted", AsyncMock(return_value=False))
    _unblocked_gate(monkeypatch)

    calls: list[str] = []

    async def _fake_backfill(_sb, _llm, tgt, *, payer_user_id, budget_blocks=None):
        calls.append(tgt.id)
        # Run 1 stops on its allowance with rows still ungraded; run 2 finishes.
        if len(calls) == 1:
            return Phase1BackfillResult(verdicts_written=40, stopped="allowance")
        return Phase1BackfillResult(verdicts_written=12, stopped=None)

    monkeypatch.setattr(poller_mod, "backfill_phase1_for_target", _fake_backfill)

    first = await poller_mod.resume_phase1_backfills(MagicMock())
    second = await poller_mod.resume_phase1_backfills(MagicMock())

    assert first["written"] == 40
    assert second["written"] == 12, "leftovers were never picked up"
    assert calls == [target.id, target.id], "the sweep must re-visit the target"


@pytest.mark.asyncio
async def test_resume_sweep_is_inert_while_the_feature_is_dark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the sweep ships dark with the feature and must not read
    anything — not even the active-target list — while disabled."""
    from app.services import poller as poller_mod

    monkeypatch.setattr(poller_mod.settings, "phase1_backfill_enabled", False)
    active = AsyncMock(return_value=[_target()])
    monkeypatch.setattr(poller_mod, "_active_targets", active)

    out = await poller_mod.resume_phase1_backfills(MagicMock())

    assert out == {"targets": 0, "resumed": 0, "written": 0, "skipped": 0, "errors": 0}
    active.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_failing_target_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-target isolation, mirroring the poll cycle's per-source isolation."""
    from app.services import poller as poller_mod

    t1 = _target()
    t2 = _target().model_copy(update={"id": "tgt-2"})
    monkeypatch.setattr(poller_mod.settings, "phase1_backfill_enabled", True)
    monkeypatch.setattr(poller_mod, "_active_targets", AsyncMock(return_value=[t1, t2]))
    monkeypatch.setattr(poller_mod, "_resolve_payer_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(poller_mod, "_global_budget_exhausted", AsyncMock(return_value=False))
    _unblocked_gate(monkeypatch)

    async def _flaky(_sb, _llm, tgt, *, payer_user_id, budget_blocks=None):
        if tgt.id == t1.id:
            raise RuntimeError("provider exploded")
        return Phase1BackfillResult(verdicts_written=7)

    monkeypatch.setattr(poller_mod, "backfill_phase1_for_target", _flaky)

    out = await poller_mod.resume_phase1_backfills(MagicMock())

    assert out["errors"] == 1
    assert out["written"] == 7, "the healthy target was skipped"


@pytest.mark.asyncio
async def test_verdicts_written_counts_rows_that_actually_landed() -> None:
    """``_write_verdicts`` swallows a failed chunk on purpose — a lost verdict
    is re-derived on a later pass and raising would fail the whole activation
    over a partial write. But the caller incremented by ``len(rows)``
    regardless, so the log could claim "150 verdict(s) written" when the write
    failed and none landed — the one number an operator would use to decide
    whether the backfill is working. Caught in review."""
    supabase = MagicMock()
    supabase.table.return_value.upsert.return_value.execute = AsyncMock(
        side_effect=RuntimeError("postgrest exploded")
    )

    rows = [
        bf._verdict_row(
            job_posting_id=f"j-{i}",
            target_id=TARGET_ID,
            promising=True,
            confidence=90,
            was_excluded=False,
        )
        for i in range(5)
    ]

    assert await bf._write_verdicts(supabase, rows) == 0


@pytest.mark.asyncio
async def test_verdicts_written_counts_a_successful_write() -> None:
    """Control for the test above: a working write must still be counted."""
    supabase = MagicMock()
    supabase.table.return_value.upsert.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[])
    )

    rows = [
        bf._verdict_row(
            job_posting_id=f"j-{i}",
            target_id=TARGET_ID,
            promising=True,
            confidence=90,
            was_excluded=False,
        )
        for i in range(5)
    ]

    assert await bf._write_verdicts(supabase, rows) == 5


async def _run_sweep_for_unsponsored_catalog_target(
    monkeypatch: pytest.MonkeyPatch, *, block_reason: str | None
) -> AsyncMock:
    """One app_active catalog target with NO active membership, so
    ``resolve_target_payers`` yields ``None`` — the app-owned catalog case."""
    from app.services import poller as poller_mod

    target = _target()
    monkeypatch.setattr(poller_mod.settings, "phase1_backfill_enabled", True)
    monkeypatch.setattr(poller_mod, "_active_targets", AsyncMock(return_value=[target]))
    monkeypatch.setattr(poller_mod, "_global_budget_exhausted", AsyncMock(return_value=False))
    _unblocked_gate(monkeypatch, reason=block_reason, payer=None)
    # An instance-key client is what a None payer actually resolves to — the
    # whole reason this can spend without a payer.
    monkeypatch.setattr(poller_mod, "_resolve_payer_client", AsyncMock(return_value=MagicMock()))

    backfill = AsyncMock(return_value=Phase1BackfillResult(verdicts_written=9))
    monkeypatch.setattr(poller_mod, "backfill_phase1_for_target", backfill)

    await poller_mod.resume_phase1_backfills(MagicMock())
    return backfill


@pytest.mark.asyncio
async def test_resume_does_not_backfill_an_unsponsored_catalog_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #945 re-review blocker.

    ``_active_targets`` is ``app_active OR any active membership``, so it
    deliberately includes APP-OWNED catalog targets. Those resolve to
    ``payer=None``, and ``_resolve_payer_client(None)`` returns the
    INSTANCE-KEY client rather than nothing — so a sweep that only checked
    "did I get a client?" would buy Phase-1 grades for targets nobody is
    pursuing, billed to the instance, in flat contradiction of
    ``grade_catalog_targets=false``, the setting whose whole purpose is
    refusing that spend.

    It is also the same shape as the passive burn this codebase already paid
    for once: target-independent work bills the instance key and is therefore
    invisible to a PER-PAYER gate unless something explicitly consults it."""
    backfill = await _run_sweep_for_unsponsored_catalog_target(
        monkeypatch, block_reason="catalog_ungraded"
    )

    backfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_does_backfill_a_catalog_target_when_the_operator_opts_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control. With ``grade_catalog_targets=true`` the gate returns
    no reason for an unsponsored target, and the sweep proceeds — so the guard
    above is respecting the policy, not just refusing everything payerless."""
    backfill = await _run_sweep_for_unsponsored_catalog_target(monkeypatch, block_reason=None)

    backfill.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_skips_a_sponsored_target_whose_payer_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep is discretionary catch-up spend, so ANY reason the gate gives
    to skip a target is a reason not to buy grades for it. There is no
    admission decision here for the transient/persistent split to matter to —
    only a spend one."""
    from app.services import poller as poller_mod

    target = _target()
    monkeypatch.setattr(poller_mod.settings, "phase1_backfill_enabled", True)
    monkeypatch.setattr(poller_mod, "_active_targets", AsyncMock(return_value=[target]))
    monkeypatch.setattr(poller_mod, "_global_budget_exhausted", AsyncMock(return_value=False))
    monkeypatch.setattr(poller_mod, "_resolve_payer_client", AsyncMock(return_value=MagicMock()))
    _unblocked_gate(monkeypatch, reason="over_daily_allowance")

    backfill = AsyncMock(return_value=Phase1BackfillResult())
    monkeypatch.setattr(poller_mod, "backfill_phase1_for_target", backfill)

    out = await poller_mod.resume_phase1_backfills(MagicMock())

    backfill.assert_not_awaited()
    assert out["skipped"] == 1


@pytest.mark.asyncio
async def test_resume_bills_the_payer_the_gate_authorised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One snapshot must answer both "may we spend on this target" and "who
    pays" — otherwise they can disagree.

    ``build_budget_gate`` already resolves the payers it gates on, so a second
    ``resolve_target_payers`` read is a separate look at ``user_targets``. A
    membership deactivated between the two leaves the gate saying "sponsored,
    unblocked" while the second read returns ``None``, and
    ``_resolve_payer_client(None)`` then bills the INSTANCE KEY for a target
    that is no longer sponsored — re-creating the unsponsored-catalog spend
    through a race. Caught in review.

    Asserted by making the two sources disagree on purpose: if the sweep ever
    re-resolves ownership, it bills ``u-STALE`` instead of the gate's answer."""
    from app.services import poller as poller_mod

    target = _target()
    monkeypatch.setattr(poller_mod.settings, "phase1_backfill_enabled", True)
    monkeypatch.setattr(poller_mod, "_active_targets", AsyncMock(return_value=[target]))
    monkeypatch.setattr(poller_mod, "_global_budget_exhausted", AsyncMock(return_value=False))
    _unblocked_gate(monkeypatch, payer="u-AUTHORISED")

    # A second ownership read would return something else entirely.
    stale = AsyncMock(return_value={target.id: "u-STALE"})
    monkeypatch.setattr(poller_mod, "resolve_target_payers", stale, raising=False)

    seen: list[str | None] = []

    async def _client(_cache, _sb, payer_user_id):
        seen.append(payer_user_id)
        return MagicMock()

    monkeypatch.setattr(poller_mod, "_resolve_payer_client", _client)
    billed: list[str | None] = []

    async def _backfill(_sb, _llm, _t, *, payer_user_id, budget_blocks=None):
        billed.append(payer_user_id)
        return Phase1BackfillResult(verdicts_written=1)

    monkeypatch.setattr(poller_mod, "backfill_phase1_for_target", _backfill)

    await poller_mod.resume_phase1_backfills(MagicMock())

    assert seen == ["u-AUTHORISED"], "client resolved for a payer the gate did not authorise"
    assert billed == ["u-AUTHORISED"], "spend attributed to a re-resolved payer"
    stale.assert_not_awaited()
