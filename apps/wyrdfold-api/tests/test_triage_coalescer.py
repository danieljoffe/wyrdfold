"""#1015 packing: concurrent same-target triage requests ride ONE call.

These drive the REAL ``triage_titles`` against ``MockLLMClient`` wherever the
question is about correctness, so packing, the real prompt, the real parsing and
the verdict split are exercised together rather than testing the split in
isolation against a stub that agrees with it.

The load-bearing property is ATTRIBUTION: caller B must never receive caller A's
verdict. A coalescer with an off-by-one in its offset arithmetic still returns
plausible verdicts to everyone, so several tests assert against each caller's
OWN titles (via the faithful variant's ``title_prefix``) rather than merely
counting what came back.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.targets import JobTarget, ScoringProfile
from app.services.llm.mock import MockLLMClient, phase1_triage_verdicts_json
from app.services.relevance.title_triage import PHASE1_PURPOSE, triage_titles
from app.services.relevance.triage_coalescer import TitleTriageCoalescer

pytestmark = pytest.mark.asyncio


def _target(tid: str = "t-1") -> JobTarget:
    return JobTarget(
        id=tid,
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(categories={}),
        search_keywords=["frontend engineer"],
        app_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _client(packed_titles: list[str], variant: str = "faithful") -> MockLLMClient:
    """A mock scripted for the PACKED batch the coalescer will send."""
    return MockLLMClient(
        scripted={PHASE1_PURPOSE: phase1_triage_verdicts_json(packed_titles, variant)}
    )


class _CountingTriage:
    """Wraps the real ``triage_titles`` and records the batches it was sent."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def __call__(self, llm: Any, *, target: JobTarget, titles: list[str]) -> Any:
        self.batches.append(list(titles))
        return await triage_titles(llm, target=target, titles=titles)

    @property
    def calls(self) -> int:
        return len(self.batches)


# ---- The point of the feature: fewer calls, same questions -----------------


async def test_concurrent_same_target_callers_ride_one_call():
    a, b, c = ["A one", "A two"], ["B one"], ["C one", "C two", "C three"]
    counting = _CountingTriage()
    co = TitleTriageCoalescer(debounce_seconds=0.02, triage=counting)
    llm = _client(a + b + c)
    tgt = _target()

    results = await asyncio.gather(
        co.submit(llm, target=tgt, titles=a),
        co.submit(llm, target=tgt, titles=b),
        co.submit(llm, target=tgt, titles=c),
    )

    assert counting.calls == 1, "three callers must produce ONE call"
    assert counting.batches[0] == a + b + c
    assert [len(r.verdicts) for r in results] == [2, 1, 3], "each gets its OWN slice back"


async def test_different_targets_are_never_merged():
    """The prompt is target-specific, so merging across targets would ask the
    wrong question — the one thing this must never do."""
    counting = _CountingTriage()
    co = TitleTriageCoalescer(debounce_seconds=0.02, triage=counting)
    t1, t2 = _target("t-1"), _target("t-2")

    await asyncio.gather(
        co.submit(_client(["X one"]), target=t1, titles=["X one"]),
        co.submit(_client(["Y one"]), target=t2, titles=["Y one"]),
    )

    assert counting.calls == 2
    assert {tuple(b) for b in counting.batches} == {("X one",), ("Y one",)}


# ---- Attribution: the failure a plausible-looking coalescer still has ------


async def test_each_caller_receives_verdicts_for_its_own_titles_only():
    """The faithful variant echoes each title's own prefix, so an offset bug
    shows up as caller B holding caller A's title."""
    a = ["Alpha engineer", "Alpha designer"]
    b = ["Bravo engineer"]
    c = ["Charlie engineer", "Charlie analyst"]
    co = TitleTriageCoalescer(debounce_seconds=0.02)
    llm = _client(a + b + c)
    tgt = _target()

    ra, rb, rc = await asyncio.gather(
        co.submit(llm, target=tgt, titles=a),
        co.submit(llm, target=tgt, titles=b),
        co.submit(llm, target=tgt, titles=c),
    )

    for result, own in ((ra, a), (rb, b), (rc, c)):
        assert sorted(result.verdicts) == list(range(1, len(own) + 1)), "1-based, local"
        for idx, verdict in result.verdicts.items():
            expected = " ".join(own[idx - 1].split()[:2])
            assert verdict.title_prefix == expected, (
                f"verdict {idx} carries {verdict.title_prefix!r}, "
                f"but this caller's title {idx} is {own[idx - 1]!r}"
            )


async def test_boundary_only_verdicts_land_in_the_right_callers():
    """``boundary_only`` answers ONLY the first and last id of the packed batch.
    Those belong to different callers, so a split bug puts both in one slice —
    the misattribution the faithful variant cannot expose."""
    a, b = ["Alpha engineer", "Alpha designer"], ["Bravo engineer", "Bravo analyst"]
    co = TitleTriageCoalescer(debounce_seconds=0.02)
    llm = _client(a + b, variant="boundary_only")
    tgt = _target()

    ra, rb = await asyncio.gather(
        co.submit(llm, target=tgt, titles=a),
        co.submit(llm, target=tgt, titles=b),
    )

    assert list(ra.verdicts) == [1], "first caller keeps only the batch's first id"
    assert list(rb.verdicts) == [2], "last id maps to the SECOND caller's index 2"
    assert ra.verdicts[1].title_prefix == "Alpha engineer"
    assert rb.verdicts[2].title_prefix == "Bravo analyst"


async def test_dropped_verdicts_stay_dropped_for_the_right_caller():
    """``omits_ids`` answers only the first half of the packed batch. The
    missing ones must be missing for the callers they belong to, so the
    poller's fail-open applies to the right titles."""
    a, b = ["Alpha one", "Alpha two"], ["Bravo one", "Bravo two"]
    co = TitleTriageCoalescer(debounce_seconds=0.02)
    llm = _client(a + b, variant="omits_ids")
    tgt = _target()

    ra, rb = await asyncio.gather(
        co.submit(llm, target=tgt, titles=a),
        co.submit(llm, target=tgt, titles=b),
    )

    assert list(ra.verdicts) == [1, 2], "first half answered → first caller whole"
    assert rb.verdicts == {}, "second half omitted → second caller gets nothing"


# ---- Cost accounting: one real call, one cost row --------------------------


async def test_exactly_one_caller_owns_the_cost_and_records_the_packed_size():
    """The per-target daily cap counts ``llm_costs`` ROWS, so N rows for one
    call would over-count the cap and N-1 phantom calls of spend."""
    a, b, c = ["A one"], ["B one", "B two"], ["C one"]
    co = TitleTriageCoalescer(debounce_seconds=0.02)
    llm = _client(a + b + c)
    tgt = _target()

    results = await asyncio.gather(
        co.submit(llm, target=tgt, titles=a),
        co.submit(llm, target=tgt, titles=b),
        co.submit(llm, target=tgt, titles=c),
    )

    owners = [r for r in results if r.owns_cost]
    assert len(owners) == 1, "exactly one cost row per real call"
    assert owners[0].batch_size == 4, "records the PACKED size, not its own slice"
    assert all(r.batch_size == 0 for r in results if not r.owns_cost)


async def test_every_caller_is_told_it_was_attempted():
    """The poller reads ``result is not None`` as 'these titles were judged'.
    Non-owners must still see it, or their titles would DEFER (#285) despite
    having been graded — silently re-spending on them next cycle."""
    a, b = ["A one"], ["B one"]
    co = TitleTriageCoalescer(debounce_seconds=0.02)
    results = await asyncio.gather(
        co.submit(_client(a + b), target=_target(), titles=a),
        co.submit(_client(a + b), target=_target(), titles=b),
    )
    assert all(r.result is not None for r in results)


# ---- Failure and cancellation ---------------------------------------------


async def test_a_failed_call_defers_every_caller_rather_than_raising():
    """``result is None`` means 'not attempted → defer'. Raising instead would
    turn a provider blip into an exception inside the poll cycle."""

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("provider exploded")

    co = TitleTriageCoalescer(debounce_seconds=0.02, triage=boom)
    tgt = _target()
    results = await asyncio.gather(
        co.submit(_client([]), target=tgt, titles=["A one"]),
        co.submit(_client([]), target=tgt, titles=["B one"]),
    )
    assert all(r.result is None and r.verdicts == {} for r in results)


async def test_a_cancelled_caller_does_not_abandon_the_others():
    """Each source is wrapped in ``asyncio.wait_for`` with its own wall-clock
    budget, so one being cancelled mid-batch is routine. The flush is owned by
    the coalescer, so the survivors must still be served."""
    a, b = ["Alpha one"], ["Bravo one"]
    counting = _CountingTriage()
    co = TitleTriageCoalescer(debounce_seconds=0.05, triage=counting)
    llm = _client(a + b)
    tgt = _target()

    doomed = asyncio.ensure_future(co.submit(llm, target=tgt, titles=a))
    survivor = asyncio.ensure_future(co.submit(llm, target=tgt, titles=b))
    await asyncio.sleep(0)
    doomed.cancel()

    result = await survivor
    assert result.verdicts, "the surviving caller still gets its verdicts"
    assert result.verdicts[1].title_prefix == "Bravo one"
    assert counting.calls == 1, "and the cancelled caller's titles still rode the call"


# ---- Batch limits and draining --------------------------------------------


async def test_a_full_batch_flushes_without_waiting_out_the_debounce():
    a = [f"Title {i}" for i in range(4)]
    counting = _CountingTriage()
    co = TitleTriageCoalescer(debounce_seconds=30.0, batch_size=4, triage=counting)
    # A 30s debounce would hang the test if the full-batch path did not fire.
    result = await asyncio.wait_for(co.submit(_client(a), target=_target(), titles=a), timeout=2.0)
    assert counting.calls == 1
    assert len(result.verdicts) == 4


async def test_accumulation_beyond_the_cap_splits_into_whole_callers():
    """Callers are never split across calls: one owner per cost row, and no
    cross-call index arithmetic."""
    a, b, c = ["A1", "A2"], ["B1", "B2"], ["C1"]
    counting = _CountingTriage()
    co = TitleTriageCoalescer(debounce_seconds=0.05, batch_size=3, triage=counting)
    tgt = _target()
    llm = MockLLMClient(
        scripted={PHASE1_PURPOSE: phase1_triage_verdicts_json(["A1", "A2"], "faithful")}
    )

    results = await asyncio.gather(
        co.submit(llm, target=tgt, titles=a),
        co.submit(llm, target=tgt, titles=b),
        co.submit(llm, target=tgt, titles=c),
    )

    assert counting.calls >= 2, "3 callers / 5 titles cannot fit one 3-title call"
    for batch in counting.batches:
        assert len(batch) <= 3, f"a call exceeded the cap: {batch}"
    # Every caller's titles appear exactly once, contiguously, in some call.
    flat = [t for batch in counting.batches for t in batch]
    assert sorted(flat) == sorted(a + b + c)
    assert sum(r.owns_cost for r in results) == counting.calls, "one owner per call"


async def test_drain_flushes_titles_still_inside_the_debounce_window():
    """Without draining, work buffered when the cycle ends is silently lost."""
    a = ["Alpha one"]
    counting = _CountingTriage()
    co = TitleTriageCoalescer(debounce_seconds=30.0, triage=counting)
    tgt = _target()

    pending = asyncio.ensure_future(co.submit(_client(a), target=tgt, titles=a))
    await asyncio.sleep(0)
    await co.drain()

    result = await asyncio.wait_for(pending, timeout=2.0)
    assert counting.calls == 1
    assert result.verdicts[1].title_prefix == "Alpha one"


async def test_empty_titles_never_reach_the_model():
    counting = _CountingTriage()
    co = TitleTriageCoalescer(debounce_seconds=0.02, triage=counting)
    result = await co.submit(_client([]), target=_target(), titles=[])
    assert counting.calls == 0
    assert result.verdicts == {} and result.result is None and not result.owns_cost


async def test_a_lost_flush_defers_rather_than_hanging_forever():
    """A waiter's future is resolved by a flush it does not own, so any bug that
    loses the flush would block the caller — and with it the poll cycle —
    indefinitely.

    Found by sabotage: keying the pending map differently from the flush made
    the suite HANG rather than fail, which in CI is worse than a red test. The
    guard degrades to the same defer the failure path uses, and logs that it is
    a coalescer bug rather than a provider failure.
    """
    co = TitleTriageCoalescer(debounce_seconds=30.0, max_wait_seconds=0.05)
    # Drop the flush on the floor, simulating a lost/never-scheduled flush.
    co._spawn_flush = lambda _target_id: None  # type: ignore[method-assign]

    result = await asyncio.wait_for(
        co.submit(_client(["A one"]), target=_target(), titles=["A one"]),
        timeout=5.0,
    )
    assert result.result is None, "degrades to defer"
    assert result.verdicts == {}
    assert not result.owns_cost, "a deferred waiter must not claim a cost row"
