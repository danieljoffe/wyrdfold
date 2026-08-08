"""In-process registry of in-flight tailoring runs (#656).

Tailoring is non-blocking, the same shape ``/analysis`` took in #459:
``POST /tailor/resume`` and ``POST /tailor/cover-letter`` hand the multi-call
LLM pipeline to a detached background task and return ``202`` immediately;
the client polls ``GET /tailor/resumes/by-job/{id}`` (or the cover-letter
sibling) until the ``documents`` row lands. This module is the transient
"is a run in flight / did it just fail" state that ties the two verbs
together.

The mechanism lives in ``app.services.run_registry``; see it for the
in-memory / **single uvicorn worker** contract. This module owns the tailor
instance, its key space, and its TTLs.

Keyed on ``(user_id, document_type, job_posting_id)`` — the same tuple
``persistence.get_by_job`` reads back, so dedup matches the poll surface
exactly. A run is only backgrounded when a ``job_posting_id`` exists, because
that id IS the poll surface; JD-only callers (operator / api-key) keep the
blocking path and need no registry entry.
"""

from __future__ import annotations

import time  # noqa: F401  (parity with the analysis shim: tests patch the clock here)

from app.models.tailor import DocumentType
from app.services.run_registry import RunRegistry, RunState, RunStatus

# Mirrors the analysis registry: long enough for the client's next poll to
# surface the failure, short enough that a stale "error" never shadows a retry.
_ERROR_TTL_S = 120.0
# Backstop for a "running" entry whose task died without clearing it. Set well
# above the analysis registry's 300s: a resume run is up to THREE LLM calls
# (generate → faithfulness review → corrective regen) plus a pandoc render and
# a Storage upload, where an analysis run is one ~26s call. Anything still
# "running" past this is stale → swept → the next poll reads ``idle`` and the
# client re-kicks.
_RUNNING_TTL_S = 900.0
_MAX_ENTRIES = 10_000

# (user_id, document_type, job_posting_id)
Key = tuple[str, str, str]

_registry = RunRegistry(
    error_ttl_s=_ERROR_TTL_S,
    running_ttl_s=_RUNNING_TTL_S,
    max_entries=_MAX_ENTRIES,
)


def key_for(*, user_id: str, document_type: DocumentType, job_posting_id: str) -> Key:
    """The registry key for one user's document of a given type on one posting.

    Scoped by ``user_id`` so one user's in-flight run can never dedup away
    another user's kick for the same (globally shared) job posting.
    """
    return (user_id, document_type, job_posting_id)


def is_running(key: Key) -> bool:
    """True iff a run for ``key`` is currently in flight. An ``error`` entry is
    NOT "running" — a retry POST re-claims it."""
    return _registry.is_running(key)


def get(key: Key) -> RunState | None:
    """Current state for ``key`` (``None`` if idle), sweeping stale entries."""
    return _registry.get(key)


def begin(key: Key, *, user_id: str | None) -> None:
    """Claim ``key`` as running. Call synchronously right after ``is_running``
    with no ``await`` between, so the check-and-set is atomic on the event loop
    and two concurrent kicks can't both spawn (double LLM spend)."""
    _registry.begin(key, user_id=user_id)


def finish(key: Key) -> None:
    """Clear ``key`` — the ``documents`` row is now the source of truth a poll
    reads. Also releases a claim when post-claim validation fails, so the key
    is immediately re-kickable."""
    _registry.finish(key)


def fail(key: Key, *, message: str) -> None:
    """Mark ``key`` failed so the client's next poll surfaces a retryable error
    instead of polling until the ceiling."""
    _registry.fail(key, message=message)


def running_count_for_user(user_id: str | None) -> int:
    """In-flight tailoring runs owned by ``user_id``.

    Gates concurrent fan-out. Backgrounding removed the natural serialization a
    39s blocking request used to impose on a single browser tab, and
    ``enforce_llm_budget`` meters *spend* — whose ``llm_costs`` rows don't
    exist until each run's LLM returns. Without this count, N simultaneous
    kicks all read the same pre-burst spend and all pass."""
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
    "key_for",
    "running_count_for_user",
]
