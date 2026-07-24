"""In-process registry of in-flight LLM job-analysis runs (#459).

Analysis is non-blocking: ``POST /analysis/{id}`` kicks off the ~26s LLM run
as a detached background task and returns ``202`` immediately; the client
polls ``GET /analysis/{id}`` until the result lands in the durable
``analyses`` cache. This module is the transient "is a run in flight / did
it just fail" state that ties the two verbs together.

Deliberately **in-memory**, for two reasons:

* The durable source of truth for the RESULT is the ``analyses`` table
  (written by the task). The registry only holds the ephemeral
  running/failed flag — losing it on a restart just means the client's next
  poll sees ``idle`` and re-kicks a fresh POST. Nothing user-visible is lost.
* The API runs a **single uvicorn worker** (``apps/wyrdfold-api/Dockerfile``
  ``CMD`` — no ``--workers``), so one process observes every POST and every
  poll: a plain module-level dict is sufficient and correct. All mutations
  happen on the single asyncio event-loop thread (the handlers and the
  detached task body call these functions directly, never inside
  ``asyncio.to_thread``), so check-and-set sequences with no ``await``
  between them are atomic without a lock. **If the API is ever scaled past
  one worker, dedup breaks** — a POST on worker A and a poll on worker B
  would not share this dict — and this must move to a shared store (a
  ``status`` column on ``analyses`` / advisory lock / Redis).

Keyed on the analysis cache key ``(job_posting_id, target_id,
optimized_doc_id)`` so dedup matches persistence exactly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

RunStatus = Literal["running", "error"]

# A failed run lingers only long enough for the client's next poll to surface
# the error, then is swept so a later retry isn't shadowed by a stale "error"
# and memory stays bounded.
_ERROR_TTL_S = 120.0
# Backstop for a "running" entry whose task died without clearing it (process
# killed mid-flight, hard cancellation). The LLM call's own timeout is far
# under this, so anything still "running" past it is stale → swept → the next
# poll reads ``idle`` and the client re-kicks. Belt to the task's try/finally
# suspenders.
_RUNNING_TTL_S = 300.0
# Defensive ceiling; real concurrent fan-out is a handful of entries.
_MAX_ENTRIES = 10_000

# (job_posting_id, target_id, optimized_doc_id)
Key = tuple[str, str, str]


@dataclass
class RunState:
    status: RunStatus
    user_id: str | None
    error: str | None = None
    updated_at: float = field(default_factory=time.monotonic)


_runs: dict[Key, RunState] = {}


def _sweep(now: float) -> None:
    """Drop expired ``error`` entries and stale ``running`` entries.

    Lazy TTL: called on every read/mutation rather than from a timer, which
    keeps the module dependency-free and the single-worker invariant intact.
    """
    expired = [
        key
        for key, st in _runs.items()
        if (st.status == "error" and now - st.updated_at > _ERROR_TTL_S)
        or (st.status == "running" and now - st.updated_at > _RUNNING_TTL_S)
    ]
    for key in expired:
        _runs.pop(key, None)


def is_running(key: Key) -> bool:
    """True iff a run for ``key`` is currently in flight.

    An ``error`` entry is NOT "running": a retry POST is allowed to re-claim
    and re-run it.
    """
    st = _runs.get(key)
    return st is not None and st.status == "running"


def get(key: Key) -> RunState | None:
    """Current state for ``key`` (``None`` if idle), sweeping stale entries."""
    _sweep(time.monotonic())
    return _runs.get(key)


def begin(key: Key, *, user_id: str | None) -> None:
    """Claim ``key`` as running.

    Call synchronously immediately after an ``is_running`` check with no
    ``await`` in between, so the check-and-set is atomic on the event loop and
    two concurrent kicks can't both claim the same key (dedup → no double
    LLM spend).
    """
    now = time.monotonic()
    _sweep(now)
    if len(_runs) >= _MAX_ENTRIES:
        # Never let a runaway registry grow unbounded; the durable result
        # still persists to ``analyses`` regardless of this flag.
        return
    _runs[key] = RunState(status="running", user_id=user_id, updated_at=now)


def finish(key: Key) -> None:
    """Clear ``key`` after a successful run — the ``analyses`` cache row is now
    the source of truth a poll reads. Also used to release a claim when
    post-claim validation fails (429/404/422) so the key is immediately
    re-kickable."""
    _runs.pop(key, None)


def fail(key: Key, *, message: str) -> None:
    """Mark ``key`` failed so the client's next poll surfaces a retryable
    error instead of polling forever."""
    st = _runs.get(key)
    user_id = st.user_id if st is not None else None
    _runs[key] = RunState(
        status="error", user_id=user_id, error=message, updated_at=time.monotonic()
    )


def running_count_for_user(user_id: str | None) -> int:
    """Number of in-flight runs owned by ``user_id``.

    Folded into the daily-count budget gate so a burst of concurrent kicks
    (each of which writes its ``llm_costs`` row only ~26s later, after the LLM
    returns) can't all pass a gate that only sees already-persisted rows and
    blow past the cap."""
    _sweep(time.monotonic())
    return sum(1 for st in _runs.values() if st.status == "running" and st.user_id == user_id)


def clear_all() -> None:
    """Test hook: reset the registry between cases."""
    _runs.clear()
