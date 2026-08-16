"""Generic in-process registry of in-flight LLM runs.

Extracted from the analysis registry (#459) when tailoring picked up the same
202+poll shape (#656). The pattern both endpoints share: a POST hands a
multi-second LLM run to a detached task and returns ``202`` at once; the
client polls a companion GET until the durable result lands. This module is
the transient "is a run in flight / did it just fail" state that ties the two
verbs together.

Deliberately **in-memory**, for two reasons:

* The durable source of truth for the RESULT is always a table (the
  ``analyses`` cache, the ``documents`` row). The registry only holds the
  ephemeral running/failed flag — losing it on a restart just means the
  client's next poll sees ``idle`` and re-kicks a fresh POST. Nothing
  user-visible is lost.
* The API runs a **single uvicorn worker** (``apps/wyrdfold-api/Dockerfile``
  ``CMD`` — no ``--workers``), so one process observes every POST and every
  poll: a plain dict is sufficient and correct. All mutations happen on the
  single asyncio event-loop thread (handlers and detached task bodies call
  these methods directly, never inside ``asyncio.to_thread``), so
  check-and-set sequences with no ``await`` between them are atomic without a
  lock. **If the API is ever scaled past one worker, dedup breaks** — a POST
  on worker A and a poll on worker B would not share this dict — and this
  must move to a shared store (a ``status`` column / advisory lock / Redis).

Each caller owns its own ``RunRegistry`` instance so the key space and the
TTLs are per-domain: an analysis run is one ~26s LLM call, a resume tailoring
is up to two (~39s, plus the faithfulness review pass), so they can't share a
single stale-entry backstop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

RunStatus = Literal["running", "error"]

# A run key is any tuple of strings; each domain documents its own shape.
Key = tuple[str, ...]


@dataclass
class RunState:
    status: RunStatus
    user_id: str | None
    error: str | None = None
    updated_at: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    """Wall-clock start, for comparing against a persisted row's ``created_at``.

    ``updated_at`` is deliberately monotonic (immune to clock steps) and so
    cannot be compared with a database timestamp. A poll needs exactly that
    comparison to tell "this run produced the row you are looking at" from
    "this run is still working and that row is its predecessor" (#788)."""


class RunRegistry:
    """One domain's in-flight run table.

    ``error_ttl_s`` — a failed run lingers only long enough for the client's
    next poll to surface the error, then is swept so a later retry isn't
    shadowed by a stale ``error`` and memory stays bounded.

    ``running_ttl_s`` — backstop for a ``running`` entry whose task died
    without clearing it (process killed mid-flight, hard cancellation). Set it
    comfortably above the run's own timeout: anything still ``running`` past it
    is stale → swept → the next poll reads ``idle`` and the client re-kicks.
    Belt to the task's try/finally suspenders.

    ``max_entries`` — defensive ceiling; real concurrent fan-out is a handful.
    """

    def __init__(
        self,
        *,
        error_ttl_s: float,
        running_ttl_s: float,
        max_entries: int = 10_000,
    ) -> None:
        self._error_ttl_s = error_ttl_s
        self._running_ttl_s = running_ttl_s
        self._max_entries = max_entries
        self._runs: dict[Key, RunState] = {}

    def _sweep(self, now: float) -> None:
        """Drop expired ``error`` entries and stale ``running`` entries.

        Lazy TTL: called on every read/mutation rather than from a timer, which
        keeps the module dependency-free and the single-worker invariant intact.
        """
        expired = [
            key
            for key, st in self._runs.items()
            if (st.status == "error" and now - st.updated_at > self._error_ttl_s)
            or (st.status == "running" and now - st.updated_at > self._running_ttl_s)
        ]
        for key in expired:
            self._runs.pop(key, None)

    def is_running(self, key: Key) -> bool:
        """True iff a run for ``key`` is currently in flight.

        An ``error`` entry is NOT "running": a retry POST is allowed to
        re-claim and re-run it.
        """
        st = self._runs.get(key)
        return st is not None and st.status == "running"

    def get(self, key: Key) -> RunState | None:
        """Current state for ``key`` (``None`` if idle), sweeping stale entries."""
        self._sweep(time.monotonic())
        return self._runs.get(key)

    def begin(self, key: Key, *, user_id: str | None) -> None:
        """Claim ``key`` as running.

        Call synchronously immediately after an ``is_running`` check with no
        ``await`` in between, so the check-and-set is atomic on the event loop
        and two concurrent kicks can't both claim the same key (dedup → no
        double LLM spend).
        """
        now = time.monotonic()
        self._sweep(now)
        if len(self._runs) >= self._max_entries:
            # Never let a runaway registry grow unbounded; the durable result
            # still persists regardless of this flag.
            return
        self._runs[key] = RunState(status="running", user_id=user_id, updated_at=now)

    def finish(self, key: Key) -> None:
        """Clear ``key`` after a successful run — the durable row is now the
        source of truth a poll reads. Also used to release a claim when
        post-claim validation fails (429/404/422) so the key is immediately
        re-kickable."""
        self._runs.pop(key, None)

    def fail(self, key: Key, *, message: str) -> None:
        """Mark ``key`` failed so the client's next poll surfaces a retryable
        error instead of polling forever."""
        st = self._runs.get(key)
        user_id = st.user_id if st is not None else None
        self._runs[key] = RunState(
            status="error",
            user_id=user_id,
            error=message,
            updated_at=time.monotonic(),
            # Carry the original start forward rather than restamping it — the
            # failed run is still the same run.
            **({"started_at": st.started_at} if st is not None else {}),
        )

    def running_count_for_user(self, user_id: str | None) -> int:
        """Number of in-flight runs owned by ``user_id``.

        Feeds the concurrency/budget gates: a burst of concurrent kicks (each
        of which writes its ``llm_costs`` row only tens of seconds later, once
        the LLM returns) would otherwise all pass a gate that only sees
        already-persisted rows."""
        self._sweep(time.monotonic())
        return sum(
            1 for st in self._runs.values() if st.status == "running" and st.user_id == user_id
        )

    def clear_all(self) -> None:
        """Test hook: reset the registry between cases."""
        self._runs.clear()
