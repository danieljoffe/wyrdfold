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
an ``async def`` route handler that runs a blocking supabase round-trip on
the loop, in *either* form —

1. a literal ``.execute()`` on the supabase-py query builder, or
2. a call to a **sync helper that takes the client** (``crud.get(supabase, …)``,
   ``prose.create_version(supabase, …)``, a ``_require_*`` ownership guard, …) —
   the helper runs its own ``.execute()`` on the loop just the same, and the
   handler body has no literal ``.execute()`` for form (1) to catch.

Form (2) is the trap the ``.execute()``-only scan missed (audit 2026-07-18
PERF-M): FastAPI threadpools a *sync def* handler, but a sync helper called
from an ``async def`` runs on the event loop — it is NOT threadpooled just for
being a plain ``def``. Both forms are caught here.

A call is treated as safe when it is **awaited** (an async call yields the loop)
or **lexically offloaded** inside ``asyncio.to_thread`` / ``spawn_detached`` /
``create_task`` / ``ensure_future`` / ``BackgroundTasks.add_task``. So these do
not false-positive:

- plain ``def`` handlers (FastAPI threadpools them — only ``async def`` handlers
  are scanned), or
- ``await asyncio.to_thread(lambda: ...execute())`` /
  ``await asyncio.to_thread(crud.get, supabase, …)`` wrapped calls, or
- ``await some_async_helper(supabase, …)`` (async, yields the loop), or
- a coroutine handed to ``spawn_detached(...)`` / ``add_task(...)``.
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


# --- Form (2): blocking sync-helper calls (the class the .execute() scan misses)
# supabase-py is sync, so any helper that takes the client and runs a query
# (``crud.*``, ``prose.*``, ``optimized.*``, ``cost_log.*``, ``_require_*`` …)
# blocks the loop when called from an ``async def`` — even with no literal
# ``.execute()`` in the handler body. Detect it structurally: a call that PASSES
# the client object and is neither awaited (→ an async call) nor lexically
# offloaded inside a known off-loop wrapper.
# Every parameter name a handler binds a supabase ``Client`` to (``Depends`` of a
# get_supabase* provider). Passing any of these to a non-offloaded, non-awaited
# call is a blocking round-trip. Keep in sync with the router signatures.
_CLIENT_ARG_NAMES = {
    "supabase",
    "caller_supabase",
    "cost_supabase",
    "service_supabase",
    "user_supabase",
}
_OFFLOAD_WRAPPERS = {
    "to_thread",
    "spawn_detached",
    "create_task",
    "ensure_future",
    "add_task",
    # ThreadPoolExecutor.submit(fn, supabase, …) / loop.run_in_executor(...) also
    # run the callable off the loop — a deliberate parallel-DB-fan-out pattern.
    "submit",
    "run_in_executor",
}


def _wrapper_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _passes_client(call: ast.Call) -> bool:
    """True when the client object is a direct positional/keyword arg."""
    if any(isinstance(a, ast.Name) and a.id in _CLIENT_ARG_NAMES for a in call.args):
        return True
    return any(
        isinstance(k.value, ast.Name) and k.value.id in _CLIENT_ARG_NAMES
        for k in call.keywords
    )


def _blocking_client_call_lines(handler: ast.AsyncFunctionDef) -> list[int]:
    """Line numbers of blocking sync-helper calls (pass the client, run on the
    loop) in ``handler`` — the helper-call twin of ``_unwrapped_execute_lines``.

    Safe (skipped): a call that is awaited (async → yields the loop) or lives
    lexically inside an off-loop wrapper (``to_thread`` / ``spawn_detached`` /
    ``create_task`` / ``ensure_future`` / ``add_task``). Only the handler body is
    scanned, so signature ``Depends(get_supabase)`` defaults never false-positive.
    """
    awaited: set[int] = set()
    for sub in ast.walk(handler):
        if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
            awaited.add(id(sub.value))

    offenders: list[int] = []

    def visit(node: ast.AST, offloaded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # nested scope: its own accounting
            child_offloaded = offloaded
            if isinstance(child, ast.Call):
                if _wrapper_name(child) in _OFFLOAD_WRAPPERS:
                    child_offloaded = True  # everything under it runs off-loop
                elif (
                    not offloaded
                    and id(child) not in awaited
                    and _passes_client(child)
                ):
                    offenders.append(child.lineno)
            visit(child, child_offloaded)

    for stmt in handler.body:
        # A nested def (e.g. a ``_snapshot()`` run via ``to_thread``) is its own
        # scope with its own accounting — same rule the ``.execute()`` scan uses.
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        visit(stmt, False)
    return sorted(offenders)


def _scan_source(source: str) -> list[tuple[str, int, list[int]]]:
    """Return ``(handler_name, def_lineno, offending_lines)`` — both a bare
    ``.execute()`` on the loop and a blocking client-passing helper call."""
    tree = ast.parse(source)
    offenders: list[tuple[str, int, list[int]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and _is_router_handler(node):
            lines = sorted(
                set(_unwrapped_execute_lines(node))
                | set(_blocking_client_call_lines(node))
            )
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
                f"{path.name}:{def_line} async def {name}() runs a blocking "
                f"supabase round-trip on the event loop (lines {exec_lines}) — a "
                f"bare .execute() or a sync helper that takes the client "
                f"(crud.* / prose.* / _require_* / …). Make the handler a plain "
                f"`def` (if it has no real await) or wrap the call in "
                f"`await asyncio.to_thread(...)`. See #107 / audit 2026-07-18."
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


# ---- Form (2): the sync-helper-call class (audit 2026-07-18 PERF-M) ----------

_BROKEN_HELPER = """
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def bad_helper(supabase, llm):
    row = crud.get(supabase, "id")   # sync helper → blocks the loop
    await llm.call()
    return row
"""

_FIXED_HELPER_TO_THREAD = """
import asyncio
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def good_helper(supabase, llm):
    row = await asyncio.to_thread(crud.get, supabase, "id")
    await llm.call()
    return row
"""

_HELPER_IN_SPAWN_DETACHED_OK = """
from fastapi import APIRouter
router = APIRouter()

@router.post("/x")
async def spawns(supabase, llm):
    await llm.call()
    spawn_detached(_pipeline(supabase, "id"), name="x")   # detached, off-loop
    return {}
"""

_AWAITED_ASYNC_HELPER_OK = """
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def awaits_async(supabase):
    return await some_async_helper(supabase, "id")   # async → yields the loop
"""

_BACKGROUND_TASK_OK = """
from fastapi import APIRouter
router = APIRouter()

@router.post("/x")
async def enqueue(supabase, background_tasks, llm):
    await llm.call()
    background_tasks.add_task(_worker, supabase, "id")   # deferred, non-blocking
    return {}
"""


def test_scanner_flags_blocking_helper_call_in_async_handler() -> None:
    offenders = _scan_source(_BROKEN_HELPER)
    assert len(offenders) == 1
    name, _def_line, lines = offenders[0]
    assert name == "bad_helper"
    assert lines  # the crud.get(supabase, ...) line was reported


def test_scanner_passes_to_thread_wrapped_helper() -> None:
    assert _scan_source(_FIXED_HELPER_TO_THREAD) == []


def test_scanner_passes_helper_in_spawn_detached() -> None:
    assert _scan_source(_HELPER_IN_SPAWN_DETACHED_OK) == []


def test_scanner_passes_awaited_async_helper() -> None:
    assert _scan_source(_AWAITED_ASYNC_HELPER_OK) == []


def test_scanner_passes_background_task_add() -> None:
    assert _scan_source(_BACKGROUND_TASK_OK) == []


_THREADPOOL_SUBMIT_OK = """
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def usage(supabase, llm):
    await llm.call()
    with ThreadPoolExecutor(max_workers=2) as pool:
        f = pool.submit(cost_log.total_spend, supabase, user_id="u")   # off-loop
        return f.result()
"""


def test_scanner_passes_threadpool_submit() -> None:
    assert _scan_source(_THREADPOOL_SUBMIT_OK) == []


_NESTED_DEF_VIA_TO_THREAD_OK = """
import asyncio
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def usage(supabase):
    def _snapshot():
        return budget.resolve(supabase, user_id="u")   # blocking, but nested
    return await asyncio.to_thread(_snapshot)   # the whole nested fn runs off-loop
"""


def test_scanner_skips_nested_def_scope() -> None:
    # A blocking call inside a nested ``def`` is that scope's own accounting —
    # here the whole nested fn is dispatched via to_thread, so nothing to flag.
    assert _scan_source(_NESTED_DEF_VIA_TO_THREAD_OK) == []
