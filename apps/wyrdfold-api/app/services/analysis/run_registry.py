"""In-process registry of in-flight LLM job-analysis runs (#459).

Analysis is non-blocking: ``POST /analysis/{id}`` kicks off the ~26s LLM run
as a detached background task and returns ``202`` immediately; the client
polls ``GET /analysis/{id}`` until the result lands in the durable
``analyses`` cache. This module is the transient "is a run in flight / did
it just fail" state that ties the two verbs together.

The mechanism itself now lives in ``app.services.run_registry`` — tailoring
picked up the same 202+poll shape in #656, and two hand-copied dicts with
independently-drifting TTLs is exactly the duplication that rots. This module
keeps its own instance (own key space, own TTLs) and re-exports the same
module-level function surface it always had, so every caller and test is
unchanged. See the shared module for the in-memory / single-worker contract:
**if the API is ever scaled past one worker, dedup breaks.**

Keyed on the analysis cache key ``(job_posting_id, target_id,
optimized_doc_id)`` so dedup matches persistence exactly.
"""

from __future__ import annotations

import time  # noqa: F401  (tests patch ``run_registry.time.monotonic``)

from app.services.run_registry import RunRegistry, RunState, RunStatus

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

_registry = RunRegistry(
    error_ttl_s=_ERROR_TTL_S,
    running_ttl_s=_RUNNING_TTL_S,
    max_entries=_MAX_ENTRIES,
)


def is_running(key: Key) -> bool:
    """True iff a run for ``key`` is currently in flight.

    An ``error`` entry is NOT "running": a retry POST is allowed to re-claim
    and re-run it.
    """
    return _registry.is_running(key)


def get(key: Key) -> RunState | None:
    """Current state for ``key`` (``None`` if idle), sweeping stale entries."""
    return _registry.get(key)


def begin(key: Key, *, user_id: str | None) -> None:
    """Claim ``key`` as running.

    Call synchronously immediately after an ``is_running`` check with no
    ``await`` in between, so the check-and-set is atomic on the event loop and
    two concurrent kicks can't both claim the same key (dedup → no double
    LLM spend).
    """
    _registry.begin(key, user_id=user_id)


def finish(key: Key) -> None:
    """Clear ``key`` after a successful run — the ``analyses`` cache row is now
    the source of truth a poll reads. Also used to release a claim when
    post-claim validation fails (429/404/422) so the key is immediately
    re-kickable."""
    _registry.finish(key)


def fail(key: Key, *, message: str) -> None:
    """Mark ``key`` failed so the client's next poll surfaces a retryable
    error instead of polling forever."""
    _registry.fail(key, message=message)


def running_count_for_user(user_id: str | None) -> int:
    """Number of in-flight runs owned by ``user_id``.

    Folded into the daily-count budget gate so a burst of concurrent kicks
    (each of which writes its ``llm_costs`` row only ~26s later, after the LLM
    returns) can't all pass a gate that only sees already-persisted rows and
    blow past the cap."""
    return _registry.running_count_for_user(user_id)


def clear_all() -> None:
    """Test hook: reset the registry between cases."""
    _registry.clear_all()


__all__ = [
    "Key",
    "RunState",
    "RunStatus",
    "begin",
    "clear_all",
    "fail",
    "finish",
    "get",
    "is_running",
    "running_count_for_user",
]
