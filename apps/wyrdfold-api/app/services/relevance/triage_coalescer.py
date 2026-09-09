"""Pack concurrent Phase-1 triage requests for the SAME target into full batches.

WHY THIS EXISTS
``phase1_daily_cap`` counts CALLS, not titles, so how many titles ride a call
decides what the cap buys. Measured over 243,507 production calls (#1015 /
#1016): **11.1 titles per call against a configured 250**, median 2, and 34% of
calls carrying exactly one title against ~538 tokens of fixed prompt overhead.

The chunking loop in ``poller.py`` is correct — it chunks by
``phase1_batch_size()``. The *unit* of batching is the problem: triage runs
inside ``_poll_one_source``, iterating ``for active_target in active_targets``,
so a batch is bounded by ONE BOARD's new titles for one target, and one board
rarely posts many roles at once. Measured per (target, 10-minute window) on
pre-cap history: **27.2 calls covering 301.8 titles from 26.9 distinct sources**,
which fully packed is 1.89 calls — a **14.4x** reduction in calls for byte-
identical questions.

WHY COALESCING RATHER THAN RESTRUCTURING THE CYCLE
Sources are polled concurrently (``POLL_CONCURRENCY`` semaphore) and each is
wrapped in ``asyncio.wait_for(_poll_one_source(...), timeout=budget)``. Moving
triage to a cycle-level pre-pass would mean re-entering per-source admission
after it, and a per-source timeout could cancel work other sources depend on.
Coalescing keeps the per-source call shape exactly as it is — a caller still
awaits its own verdicts — and packs across the callers that happen to be in
flight together. The flush is owned by this object, never by a caller, so a
cancelled source cannot abandon another source's titles.

WHY PER TARGET IS THE SAFE KEY
``triage_titles(llm, *, target, titles)`` takes no source argument: the prompt
is built from target + titles alone, so merging across sources asks byte-
identical questions and returns identical verdicts. And the payer is resolved
per target (``payer = gate.payer_for(active_target.id)`` in the poller), so
every request merged into one call shares a payer and a BYOK key — cost
attribution stays unambiguous, which is the property that makes this sound.

COST ACCOUNTING — OWNED HERE, NOT BY A CALLER
One real LLM call must produce exactly one ``llm_costs`` row, or the per-target
daily cap (which counts rows) would over- or under-count.

An earlier design handed ``owns_cost=True`` to one waiter and let it write the
row. That loses rows under cancellation, which is ROUTINE here: every source is
wrapped in ``asyncio.wait_for`` with its own wall-clock budget, so the owning
waiter can be cancelled while the LLM call it started keeps running. Reproduced
before fixing — the provider bills the call and we record ZERO rows, undercounting
both spend and the cap. Picking the first *live* waiter only narrows the window:
a waiter can still be cancelled after ownership is handed to it and before it
writes. Raised in review of #1025.

So the coalescer persists the cost itself, inside the same task that made the
call, via the ``on_cost`` callback handed to the constructor. No waiter can drop
it, because no waiter is responsible for it. ``owns_cost`` remains on the result
purely so the un-coalesced caller path and tests can distinguish the two modes;
when ``on_cost`` is set it is always False.

Every waiter in a successful call still receives a non-None ``result``, because
the poller uses ``result is not None`` to mean "these titles were attempted" —
the fail-open/defer contract (#285/#294) — and their titles genuinely were judged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.models.llm import LLMResult
from app.models.targets import JobTarget
from app.services.llm.client import LLMClient
from app.services.relevance.title_triage import (
    TitleVerdict,
    phase1_batch_size,
    triage_titles,
)

logger = logging.getLogger(__name__)


@dataclass
class CoalescedVerdicts:
    """One caller's slice of a packed call.

    ``verdicts`` is keyed 1-based into the CALLER's own ``titles``, exactly as
    an un-coalesced ``triage_titles`` would return, so callers need no index
    arithmetic of their own.
    """

    verdicts: dict[int, TitleVerdict]
    result: LLMResult | None
    #: True for exactly ONE waiter per real LLM call — that waiter records the
    #: cost row. False for the others, whose titles rode the same call.
    owns_cost: bool = False
    #: The PACKED batch size to record on the cost row (all merged titles), not
    #: this caller's slice. Meaningless unless ``owns_cost``.
    batch_size: int = 0


@dataclass
class _Waiter:
    titles: list[str]
    future: asyncio.Future[CoalescedVerdicts]
    llm: LLMClient
    target: JobTarget


@dataclass
class _Pending:
    waiters: list[_Waiter] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None

    @property
    def title_count(self) -> int:
        return sum(len(w.titles) for w in self.waiters)


class TitleTriageCoalescer:
    """Batches concurrent same-target triage requests.

    Not a cache and not a queue: it holds a request only for ``debounce``
    seconds, and flushes immediately once a full batch is available. A caller
    always gets its own verdicts back and never sees another caller's titles.
    """

    def __init__(
        self,
        *,
        debounce_seconds: float = 0.25,
        batch_size: int | None = None,
        max_wait_seconds: float = 120.0,
        triage: object = None,
        on_cost: Callable[[JobTarget, object, int], Awaitable[None]] | None = None,
    ) -> None:
        # Called once per REAL LLM call, from inside the flush task, with
        # (target, result, packed_batch_size). Owning persistence here rather
        # than handing it to a waiter is what makes cancellation safe — see the
        # module docstring. None keeps the old caller-records behaviour, which
        # the tests use to assert the distinction.
        self._on_cost = on_cost
        self._debounce = debounce_seconds
        # A waiter must never block forever. Its future is resolved by a flush
        # it does not own, so any bug that loses the flush — a pending key that
        # stops matching the flush key, a task dying before it resolves anyone —
        # would hang the caller, and through it the whole poll cycle, which is
        # far worse than an error. Found by sabotaging the target key: waiters
        # queued under one key while the timer flushed another, and the test
        # HUNG instead of failing.
        #
        # Sized to bound a hang, NOT the latency: a real triage call takes
        # seconds, so this must sit well above it. Firing means a bug, and it
        # degrades to the same defer the failure path uses.
        self._max_wait = max_wait_seconds
        self._batch_size = batch_size
        # Injectable so the whole packing/splitting contract is testable without
        # an LLM. Defaults to the real call.
        self._triage = triage or triage_titles
        self._pending: dict[str, _Pending] = {}
        self._flushing: set[asyncio.Task[None]] = set()

    def _cap(self, llm: LLMClient) -> int:
        if self._batch_size is not None:
            return self._batch_size
        return phase1_batch_size(getattr(llm, "model", None))

    async def submit(
        self, llm: LLMClient, *, target: JobTarget, titles: list[str]
    ) -> CoalescedVerdicts:
        """Enqueue ``titles`` for ``target`` and await this caller's verdicts."""
        if not titles:
            return CoalescedVerdicts(verdicts={}, result=None)

        loop = asyncio.get_running_loop()
        waiter = _Waiter(titles=list(titles), future=loop.create_future(), llm=llm, target=target)
        pending = self._pending.setdefault(target.id, _Pending())
        pending.waiters.append(waiter)

        # Full batch already available → flush now rather than wait out the
        # debounce. A caller that arrives with a large slice must not be held.
        if pending.title_count >= self._cap(llm):
            self._cancel_timer(pending)
            self._spawn_flush(target.id)
        elif pending.timer is None:
            pending.timer = loop.call_later(self._debounce, lambda: self._spawn_flush(target.id))

        try:
            return await asyncio.wait_for(waiter.future, timeout=self._max_wait)
        except TimeoutError:
            logger.error(
                "phase1 coalescer: no flush resolved a waiter for target %s within %.0fs "
                "(%d titles) — deferring. This is a bug in the coalescer, not a "
                "provider failure.",
                target.id,
                self._max_wait,
                len(titles),
            )
            return CoalescedVerdicts(verdicts={}, result=None)

    @staticmethod
    def _cancel_timer(pending: _Pending) -> None:
        if pending.timer is not None:
            pending.timer.cancel()
            pending.timer = None

    def _spawn_flush(self, target_id: str) -> None:
        pending = self._pending.pop(target_id, None)
        if pending is None or not pending.waiters:
            return
        self._cancel_timer(pending)
        # Owned by the coalescer, never by a caller: a source cancelled by its
        # own wall-clock budget must not abandon the titles of the sources
        # batched alongside it.
        task = asyncio.create_task(self._flush(pending))
        self._flushing.add(task)
        task.add_done_callback(self._flushing.discard)

    async def _flush(self, pending: _Pending) -> None:
        cap = self._cap(pending.waiters[0].llm)
        for group in _chunk_waiters(pending.waiters, cap):
            await self._run_group(group)

    async def _run_group(self, group: list[_Waiter]) -> None:
        titles: list[str] = []
        for w in group:
            titles.extend(w.titles)
        lead = group[0]
        try:
            verdicts, result = await self._triage(  # type: ignore[operator]
                lead.llm, target=lead.target, titles=titles
            )
        except Exception:
            logger.exception(
                "phase1 coalesced triage failed for target %s (%d titles, %d callers)",
                lead.target.id,
                len(titles),
                len(group),
            )
            self._fail(group)
            return

        # Persist the cost HERE, before resolving anyone. The call happened and
        # is billed regardless of who is still waiting for it, so the row must
        # not depend on a waiter surviving (review of #1025). Its own try/except:
        # a cost-write failure must not cost the verdicts, exactly as the
        # un-coalesced path swallows the same failure.
        if result is not None and self._on_cost is not None:
            try:
                await self._on_cost(lead.target, result, len(titles))
            except Exception:
                logger.exception(
                    "phase1 coalescer: failed to record cost for target %s "
                    "(%d titles) — verdicts still delivered",
                    lead.target.id,
                    len(titles),
                )

        # Split the 1-based global verdict map back into each caller's own
        # 1-based space. A dropped verdict stays dropped for that caller, which
        # is the same fail-open the un-coalesced path has.
        #
        # ``owns_cost`` is only handed out when nobody else is persisting: with
        # ``on_cost`` set the coalescer already wrote the row, and a waiter that
        # also wrote one would double-count the cap.
        caller_records = self._on_cost is None
        offset = 0
        for i, w in enumerate(group):
            local = {
                idx - offset: v
                for idx, v in verdicts.items()
                if offset < idx <= offset + len(w.titles)
            }
            if not w.future.done():
                owns = caller_records and i == 0 and result is not None
                w.future.set_result(
                    CoalescedVerdicts(
                        verdicts=local,
                        result=result,
                        owns_cost=owns,
                        batch_size=len(titles) if owns else 0,
                    )
                )
            offset += len(w.titles)

    @staticmethod
    def _fail(group: list[_Waiter]) -> None:
        """A failed call leaves every caller with ``result=None``.

        Deliberately not an exception: the poller reads ``result is None`` as
        "not attempted → DEFER, re-triage next cycle" rather than fail-open
        admit (#285). Raising here would change that contract.
        """
        for w in group:
            if not w.future.done():
                w.future.set_result(CoalescedVerdicts(verdicts={}, result=None))

    async def drain(self) -> None:
        """Flush everything still buffered and wait for in-flight calls.

        The cycle must call this before it finishes, or titles held for the
        debounce window would be silently dropped at shutdown.
        """
        for target_id in list(self._pending):
            self._spawn_flush(target_id)
        while self._flushing:
            await asyncio.gather(*list(self._flushing), return_exceptions=True)


def _chunk_waiters(waiters: list[_Waiter], cap: int) -> list[list[_Waiter]]:
    """Group whole waiters into calls of at most ``cap`` titles.

    A waiter is never split across two calls: callers already chunk to
    ``phase1_batch_size()``, and keeping a caller whole means one owner per
    cost row and no cross-call index arithmetic. A single waiter larger than
    ``cap`` gets its own call — ``triage_titles`` will reject it, which is the
    same defensive error the un-coalesced path raises.
    """
    groups: list[list[_Waiter]] = []
    current: list[_Waiter] = []
    size = 0
    for w in waiters:
        if current and size + len(w.titles) > cap:
            groups.append(current)
            current, size = [], 0
        current.append(w)
        size += len(w.titles)
    if current:
        groups.append(current)
    return groups
