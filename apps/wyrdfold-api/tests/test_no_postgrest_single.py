"""CI guard: no ``.single()`` on a PostgREST query.

PostgREST answers ``.single()`` on zero rows with HTTP 406 / ``PGRST116``
("Cannot coerce the result to a single JSON object"), which postgrest-py raises
as ``APIError``. That makes the guard everyone writes —

    resp = await q.eq("id", x).single().execute()
    if not resp.data:          # <- UNREACHABLE
        return None

— dead code, and a missing row escapes as a 500 instead of the intended 404 /
``None``.

This wasn't hypothetical. It shipped and ran in production across **nine** call
sites until the #656 release gate drove the running system:

* ``GET /tailor/resumes/{id}`` + ``/versions`` + ``/download``, ``PATCH``,
  ``checkpoint``, ``approve``, ``unapprove``, ``export-zip`` (via
  ``persistence.get``)
* ``POST /jobs/{id}/status`` (``status.py`` — an existence probe whose 404 could
  never fire)
* ``apply_staged_patch`` — whose docstring promised "Returns None if no staged
  row matches", which ``.single()`` made impossible

**Unit tests are structurally incapable of catching it**: they mock the client
and hand back ``data=None``, which the real driver never produces. Only a live
drive or this static scan will.

Use ``app.services.db_read.fetch_one`` / ``fetch_one_sync`` instead — see that
module for why ``.maybe_single()`` isn't the answer either.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

# Nothing is exempt today. If a future call site genuinely wants "raise when the
# row is missing", add it here WITH a comment justifying it — and make sure the
# caller actually handles the APIError, rather than leaving a dead `if not
# resp.data` behind.
_ALLOWLIST: set[str] = set()


def _python_sources() -> list[Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _single_calls(source: str) -> list[int]:
    """Line numbers of real ``x.single()`` call expressions.

    Parsed, not grepped: the fixes deliberately *explain* ``.single()`` in
    docstrings and comments, and a text scan flags its own documentation. An
    AST walk only sees calls that actually execute.
    """
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "single"
        and not node.args
        and not node.keywords
    ]


def test_no_postgrest_single_calls() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        rel = str(path.relative_to(APP_ROOT.parent))
        if rel in _ALLOWLIST:
            continue
        lines = path.read_text().splitlines()
        for lineno in _single_calls("\n".join(lines)):
            offenders.append(f"{rel}:{lineno}: {lines[lineno - 1].strip()}")

    assert not offenders, (
        "PostgREST `.single()` raises APIError (PGRST116) on zero rows, so any "
        "`if not resp.data` guard after it is unreachable and a missing row 500s "
        "instead of 404ing.\n"
        "Use `app.services.db_read.fetch_one` (or `fetch_one_sync`) instead.\n\n"
        + "\n".join(offenders)
    )


def test_guard_would_catch_a_regression() -> None:
    """The guard's own negative — a scan that can't fail is worthless.

    Pins the detector against the exact shape it must catch and the prose it
    must not, so it can't quietly rot into a no-op.
    """
    assert _single_calls('resp = await q.eq("id", x).single().execute()') == [1]
    assert _single_calls("x = (\n    q\n    .single()\n)") == [2]
    # Prose and comments mentioning it must NOT trip the guard — this file and
    # db_read.py both explain the trap at length.
    assert _single_calls('"""Never use .single() — it raises on zero rows."""') == []
    assert _single_calls("# uses .limit(1), not .single()") == []
    assert _single_calls("single_row = rows[0]") == []
    assert _single_calls("maybe_single_thing()") == []


def test_fetch_one_returns_none_on_empty_and_row_when_present() -> None:
    """The replacement's contract, against the response shapes PostgREST really
    produces for a ``.limit(1)`` read: ``data=[]`` and ``data=[row]``."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.services.db_read import fetch_one, fetch_one_sync

    q = MagicMock()
    q.limit.return_value.execute = AsyncMock(return_value=MagicMock(data=[]))
    assert asyncio.run(fetch_one(q)) is None
    q.limit.assert_called_with(1)

    q2 = MagicMock()
    q2.limit.return_value.execute = AsyncMock(return_value=MagicMock(data=[{"id": "x"}]))
    assert asyncio.run(fetch_one(q2)) == {"id": "x"}

    s = MagicMock()
    s.limit.return_value.execute = MagicMock(return_value=MagicMock(data=[]))
    assert fetch_one_sync(s) is None
    s.limit.return_value.execute = MagicMock(return_value=MagicMock(data=[{"id": "y"}]))
    assert fetch_one_sync(s) == {"id": "y"}
