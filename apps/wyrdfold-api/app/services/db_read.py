"""Single-row PostgREST reads that actually return ``None`` when nothing matches.

**Never use ``.single()``.** PostgREST answers ``.single()`` on zero rows by
returning HTTP 406 with ``PGRST116`` ("Cannot coerce the result to a single JSON
object"), which postgrest-py raises as ``APIError``. So the guard everyone
writes —

    resp = await q.eq("id", x).single().execute()
    if not resp.data:          # <- UNREACHABLE
        return None

— never runs, and a missing row escapes as a 500 instead of the 404 / ``None``
the caller intended. That shipped: it was live in prod across nine call sites
(``GET /tailor/resumes/{id}`` and friends, ``POST /jobs/{id}/status``,
``apply_staged_patch``…) until the #656 release gate drove the running system
and caught it. Unit tests can't: they mock the client and hand back
``data=None``, which the real driver never produces.

``.maybe_single()`` is closer but has its own wart — postgrest-py can return
``None`` for the *response object itself*, not just ``data=None``, so every
call site needs two null checks (see ``routers/jobs.py``'s ``list_min_score``
read, which does exactly that). One missed check reintroduces the same class.

So: ``.limit(1)`` and read the row list. Always a response, always a list,
one obvious empty case. ``tests/test_no_postgrest_single.py`` enforces it.
"""

from __future__ import annotations

from typing import Any, cast


async def fetch_one(query: Any) -> dict[str, Any] | None:
    """Await a PostgREST query expecting at most one row; ``None`` if there is none.

    Pass the query *unexecuted* and without a limit — this appends
    ``.limit(1)`` itself:

        row = await fetch_one(supabase.table("jobs").select("id").eq("id", job_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Posting not found")
    """
    resp = await query.limit(1).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0] if rows else None


def fetch_one_sync(query: Any) -> dict[str, Any] | None:
    """Blocking twin of :func:`fetch_one`, for the sync-client paths that remain
    (scripts + the not-yet-converted callers in ``derive_profile``). Same
    contract; never call it from an ``async def`` handler — see
    ``tests/test_no_blocking_supabase_in_async_handlers.py``."""
    resp = query.limit(1).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return rows[0] if rows else None
