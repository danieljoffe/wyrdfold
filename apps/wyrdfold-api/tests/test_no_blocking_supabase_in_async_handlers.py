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

A call is treated as safe when it is **awaited** (an async call — or a single
``await asyncio.to_thread(...)`` — yields the loop) or handed to a **detached-async**
scheduler (``spawn_detached`` / ``create_task`` / ``ensure_future`` /
``BackgroundTasks.add_task``), which runs a coroutine on the async client off the
request's critical path. So these do not false-positive:

- plain ``def`` handlers (FastAPI threadpools them — only ``async def`` handlers
  are scanned), or
- ``await asyncio.to_thread(lambda: ...execute())`` (a bare ``.execute()`` offloaded
  to a thread — form 1), or
- ``await some_async_helper(supabase, …)`` / ``await asyncio.to_thread(crud.get,
  supabase, …)`` (awaited → yields the loop), or
- a coroutine handed to ``spawn_detached(...)`` / ``add_task(...)``.

**#57 PR-H (fully-async posture).** Now that every DB path has an async client, a
*non-awaited threaded fan-out* of a client-passing helper is no longer waved
through: ``asyncio.gather(asyncio.to_thread(crud.get, supabase), …)``,
``pool.submit(fn, supabase)``, ``loop.run_in_executor(None, fn, supabase)``. Sync
helpers on the thread pool are the thread-pool-bound blocking the migration
removed — parallelism is ``asyncio.gather`` over async calls. (The bare-``.execute()``
``to_thread`` offload in form 1 is unchanged; the single ``await asyncio.to_thread``
escape hatch stays, permitted by the awaited check.)
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
ROUTERS_DIR = APP_DIR / "routers"


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
    "sync_supabase",
    "user_supabase",
}
# Only the **detached-async** schedulers wave a client-passing call through: each
# runs a *coroutine* (on the async client) off the request's critical path. The
# **threaded** wrappers — ``to_thread`` / ``submit`` / ``run_in_executor`` — were
# removed in #57 PR-H: a sync helper on the thread pool is exactly the
# thread-pool-bound DB the async migration eliminated. The one legitimate
# ``await asyncio.to_thread(crud.get, supabase, …)`` escape hatch is still allowed,
# but by the *awaited* check in ``_blocking_client_call_lines`` (an await yields the
# loop), not by wrapper name — so a *non-awaited* threaded fan-out
# (``gather(asyncio.to_thread(fn, supabase), …)``, ``pool.submit(fn, supabase)``)
# is now flagged. Every DB path has an async client; parallelism is
# ``asyncio.gather`` over async calls, not a sync helper on the thread pool.
_OFFLOAD_WRAPPERS = {
    "spawn_detached",
    "create_task",
    "ensure_future",
    "add_task",
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
        isinstance(k.value, ast.Name) and k.value.id in _CLIENT_ARG_NAMES for k in call.keywords
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
                elif not offloaded and id(child) not in awaited and _passes_client(child):
                    offenders.append(child.lineno)
            visit(child, child_offloaded)

    for stmt in handler.body:
        # A nested def (e.g. a ``_snapshot()`` run via ``to_thread``) is its own
        # scope with its own accounting — same rule the ``.execute()`` scan uses.
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        visit(stmt, False)
    return sorted(offenders)


def _scan_source(
    source: str,
    should_scan: Callable[[ast.AsyncFunctionDef], bool] = _is_router_handler,
) -> list[tuple[str, int, list[int]]]:
    """Return ``(fn_name, def_lineno, offending_lines)`` — both a bare
    ``.execute()`` on the loop and a blocking client-passing helper call.

    ``should_scan`` selects which ``async def``s to check. It defaults to router
    handlers; the background-task scan (below) passes a name predicate to reuse
    the exact same blocking-call detection on service-layer bg functions.
    """
    tree = ast.parse(source)
    offenders: list[tuple[str, int, list[int]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and should_scan(node):
            lines = sorted(
                set(_unwrapped_execute_lines(node)) | set(_blocking_client_call_lines(node))
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

    assert not failures, "Blocking supabase call(s) on the event loop:\n" + "\n".join(failures)


# ---- Form (3): async background-task functions (add_task / spawn_detached
# ---- targets). FastAPI *awaits* an async bg task on the event loop rather than
# ---- threadpooling it (only a plain `def` bg task is threadpooled), so a
# ---- blocking supabase call inside one freezes every concurrent request — the
# ---- same failure as an async route handler, but in app/services where the
# ---- router scan above never looks. The detection is identical; only the set
# ---- of scanned functions differs.
#
# Scoped, for now, to the target-derivation module's bg functions (the ones the
# 2026-07-21 hardening review threaded). The post-feedback learner chain is now
# threaded too (``services.feedback.run_learner_and_rescore_off_loop``, #57
# PR-G2d-a). Broadening to the poll / discovery bg tasks (run_force_poll_locked,
# run_discovery_all_targets_locked) needs each audited/threaded first, so it's a
# follow-up — adding them here before that would just flip this guard red.
_BG_TASK_MODULES: dict[Path, frozenset[str]] = {
    APP_DIR / "services" / "targets" / "from_input.py": frozenset(
        {"derive_url_target_bg", "derive_manual_target_bg", "_apply_fit_score"}
    ),
}


def _async_def_names(source: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef)}


def test_no_blocking_supabase_in_background_tasks() -> None:
    failures: list[str] = []
    for path, names in _BG_TASK_MODULES.items():
        assert path.exists(), f"bg-task module moved? {path}"
        source = path.read_text()
        # Guard the guard: if a tracked function is renamed/removed, fail loudly
        # rather than silently covering nothing (offender-only scans can't do this,
        # since a correctly-threaded function has no offending lines to report).
        missing = names - _async_def_names(source)
        assert not missing, f"tracked bg functions not found in {path.name}: {sorted(missing)}"
        for name, def_line, exec_lines in _scan_source(source, lambda n, ns=names: n.name in ns):
            failures.append(
                f"{path.name}:{def_line} async def {name}() runs a blocking supabase "
                f"round-trip on the event loop (lines {exec_lines}). It is an async "
                f"BackgroundTask (add_task awaits it on the loop), so wrap each blocking "
                f"call in `await asyncio.to_thread(...)`. See #107."
            )

    assert not failures, "Blocking supabase call(s) in background tasks:\n" + "\n".join(failures)


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


# ---- Form (3) self-test: the bg-task function scan (name predicate) -----------

_BG_TASK_SOURCE = """
import asyncio

async def worker_bg(supabase, llm):
    await llm.call()
    crud.update(supabase, "t", body)                       # blocks the loop

async def clean_bg(supabase, llm):
    await llm.call()
    await asyncio.to_thread(crud.update, supabase, "t", body)   # offloaded — safe

async def not_tracked_bg(supabase):
    crud.update(supabase, "t", body)                       # blocks, but not scanned
"""


def test_bg_scan_flags_blocking_call_only_in_tracked_functions() -> None:
    tracked = {"worker_bg", "clean_bg"}
    offenders = {
        name for name, _l, _e in _scan_source(_BG_TASK_SOURCE, lambda n: n.name in tracked)
    }
    # worker_bg blocks and is tracked → flagged; clean_bg is threaded → clean;
    # not_tracked_bg blocks but is outside the tracked set → not scanned.
    assert offenders == {"worker_bg"}


# #57 PR-H: a non-awaited threaded fan-out of a client-passing helper via
# ``ThreadPoolExecutor.submit`` is thread-pool-bound DB — no longer waved through.
_THREADPOOL_SUBMIT_FLAGGED = """
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def usage(supabase, llm):
    await llm.call()
    with ThreadPoolExecutor(max_workers=2) as pool:
        f = pool.submit(cost_log.total_spend, supabase, user_id="u")   # thread-pool DB
        return f.result()
"""


def test_scanner_flags_nonawaited_threadpool_submit() -> None:
    offenders = _scan_source(_THREADPOOL_SUBMIT_FLAGGED)
    assert len(offenders) == 1
    name, _def_line, lines = offenders[0]
    assert name == "usage"
    assert lines  # the pool.submit(..., supabase, ...) line was reported


# #57 PR-H: a ``gather`` of ``asyncio.to_thread(helper, supabase)`` — the individual
# to_thread calls are NOT awaited (only the gather is), so they run the sync helper
# on the thread pool. Flagged: use ``asyncio.gather`` over *async* helpers instead.
_GATHER_TO_THREAD_DB_FLAGGED = """
import asyncio
from fastapi import APIRouter
router = APIRouter()

@router.get("/x")
async def fanout(supabase):
    a, b = await asyncio.gather(
        asyncio.to_thread(crud.get, supabase, "a"),
        asyncio.to_thread(crud.get, supabase, "b"),
    )
    return [a, b]
"""


def test_scanner_flags_gather_of_to_thread_db() -> None:
    offenders = _scan_source(_GATHER_TO_THREAD_DB_FLAGGED)
    assert len(offenders) == 1
    assert offenders[0][0] == "fanout"
    assert len(offenders[0][2]) == 2  # both to_thread(crud.get, supabase, …) lines


# The single ``await asyncio.to_thread(crud.get, supabase, …)`` escape hatch is
# STILL permitted — the awaited check yields the loop (see _FIXED_HELPER_TO_THREAD /
# test_scanner_passes_to_thread_wrapped_helper above). PR-H only tightens the
# *non-awaited* threaded fan-out, not the awaited single-call offload.


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
