"""CI guard (#107 / audit #29): no blocking supabase ``.execute()`` on the loop.

supabase-py is synchronous. A bare ``.execute()`` round-trip inside an
``async def`` route handler runs *on the event loop* and freezes every other
concurrent request in the single-process uvicorn worker until it returns.

The repo convention (#107):

- A handler whose body has no real ``await`` of its own should be a plain
  ``def`` — FastAPI runs it in its threadpool, so the blocking calls never
  touch the loop.
- A handler that genuinely needs ``await`` (e.g. an LLM call) but also makes
  blocking supabase calls keeps ``async def`` and wraps each blocking call in
  ``await asyncio.to_thread(...)``.

This test statically scans every router module for the regression class:
an ``async def`` route handler containing a synchronous supabase
``.execute()`` that is NOT lexically wrapped in ``asyncio.to_thread(...)``.

It deliberately keys on the literal ``.execute()`` terminator of the
supabase-py query builder — the unambiguous "this round-trips to Postgres
now" call — so it never false-positives on:

- plain ``def`` handlers (FastAPI threadpools them), or
- ``await asyncio.to_thread(lambda: ....execute())`` wrapped calls, or
- async handlers whose only blocking work is delegated to threadpooled
  plain-``def`` helpers (those have no literal ``.execute()`` in the handler
  body, and the helper itself is threadpooled when called from sync code).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTERS_DIR = Path(__file__).resolve().parent.parent / "app" / "routers"


def _is_router_handler(node: ast.AsyncFunctionDef) -> bool:
    """True when the function is decorated with ``@router.<method>(...)``."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "router"
        ):
            return True
    return False


def _is_execute_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    )


def _is_to_thread_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "to_thread"
    )


def _unwrapped_execute_lines(handler: ast.AsyncFunctionDef) -> list[int]:
    """Line numbers of ``.execute()`` calls in ``handler`` not under to_thread.

    Walks the handler body but does NOT descend into nested function or
    async-function definitions (e.g. an inner SSE ``generate()`` generator),
    since those are separate scopes evaluated later — they have their own
    blocking/await accounting. Any ``.execute()`` lexically inside an
    ``asyncio.to_thread(...)`` argument is considered safely offloaded.
    """

    # Collect every ``.execute()`` that lives inside a ``to_thread(...)`` call
    # anywhere in the handler — those are safe.
    safe_lines: set[int] = set()
    for sub in ast.walk(handler):
        if _is_to_thread_call(sub):
            for inner in ast.walk(sub):
                if _is_execute_call(inner):
                    safe_lines.add(inner.lineno)

    unwrapped: list[int] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            # Don't cross into a nested function scope.
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _is_execute_call(child) and child.lineno not in safe_lines:
                unwrapped.append(child.lineno)
            visit(child)

    visit(handler)
    return sorted(unwrapped)


def _scan_source(source: str) -> list[tuple[str, int, list[int]]]:
    """Return ``(handler_name, def_lineno, execute_lines)`` offenders."""
    tree = ast.parse(source)
    offenders: list[tuple[str, int, list[int]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and _is_router_handler(node):
            lines = _unwrapped_execute_lines(node)
            if lines:
                offenders.append((node.name, node.lineno, lines))
    return offenders


def test_no_blocking_supabase_execute_in_async_handlers() -> None:
    router_files = sorted(ROUTERS_DIR.glob("*.py"))
    assert router_files, f"no router modules found under {ROUTERS_DIR}"

    failures: list[str] = []
    for path in router_files:
        for name, def_line, exec_lines in _scan_source(path.read_text()):
            failures.append(
                f"{path.name}:{def_line} async def {name}() has a blocking "
                f"supabase .execute() (lines {exec_lines}) not wrapped in "
                f"asyncio.to_thread(...). Make the handler a plain `def` (if it "
                f"has no real await) or wrap the call in "
                f"`await asyncio.to_thread(...)`. See #107."
            )

    assert not failures, "Blocking supabase call(s) on the event loop:\n" + "\n".join(
        failures
    )


# ---- Self-tests: prove the scanner catches the regression and doesn't
# ---- false-positive on the legitimate patterns. -----------------------------

_BROKEN = """
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def bad_handler(supabase):
    return supabase.table("jobs").select("id").execute()
"""

_FIXED_DEF = """
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
def good_sync_handler(supabase):
    return supabase.table("jobs").select("id").execute()
"""

_FIXED_TO_THREAD = """
import asyncio
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def good_async_handler(supabase, llm):
    rows = await asyncio.to_thread(
        lambda: supabase.table("jobs").select("id").execute()
    )
    await llm.call()
    return rows
"""

_NESTED_GENERATOR_OK = """
import asyncio
from fastapi import APIRouter
router = APIRouter()

@router.post("/stream")
async def streamer(supabase):
    pre = await asyncio.to_thread(lambda: supabase.table("a").select("*").execute())

    async def generate():
        # A nested generator is its own scope; the handler proper is clean.
        await asyncio.to_thread(lambda: supabase.table("b").select("*").execute())
        yield b"x"

    return generate()
"""


def test_scanner_flags_bare_execute_in_async_handler() -> None:
    offenders = _scan_source(_BROKEN)
    assert len(offenders) == 1
    name, _def_line, exec_lines = offenders[0]
    assert name == "bad_handler"
    assert exec_lines  # the .execute() line was reported


def test_scanner_passes_plain_def_handler() -> None:
    assert _scan_source(_FIXED_DEF) == []


def test_scanner_passes_to_thread_wrapped_async_handler() -> None:
    assert _scan_source(_FIXED_TO_THREAD) == []


def test_scanner_passes_to_thread_wrapped_in_nested_generator() -> None:
    # Both the handler-level and nested-generator .execute()s are to_thread
    # wrapped, so there is nothing to flag.
    assert _scan_source(_NESTED_GENERATOR_OK) == []
