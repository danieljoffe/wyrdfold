"""#604: the candidate-window indexes exist and can serve the window's ORDER BY.

The /jobs two-query fallback draws each tier's bounded window ``ORDER BY
<tier column> DESC NULLS LAST, job_posting_id DESC LIMIT n``. Migration
``20260905000000`` adds one partial ordered index per tier so that scan is
LIMIT-driven (prod measured 2.8s / ~89k buffers per window without them).

Two properties are pinned here, because both failed silently during the
measurement pass:

* **Existence** — a migration that never ships leaves the query correct but
  slow; nothing else in CI would notice.
* **Order-serving** — an index declared ``DESC`` without ``NULLS LAST`` LOOKS
  right and still matches the partial predicate, but is ``NULLS FIRST`` under
  Postgres defaults and cannot serve the query's order: the planner quietly
  reinstates the sort node over the whole candidate set. The EXPLAIN check
  proves the plan shape, not just the catalog entry. Plan-shape is forced
  deterministic (seqscan/bitmapscan off) so it holds on the empty CI schema:
  order-serving is a structural property of the index definition, and
  ``enable_sort = off`` stops the empty-table cost tie from letting an
  unordered index + sort win arbitrarily (enable flags are soft, so the
  sabotage case — no order-serving index — still shows its Sort node).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration

PGHOST = os.environ.get("SUPABASE_TEST_DB_HOST", "127.0.0.1")
PGPORT = os.environ.get("SUPABASE_TEST_DB_PORT", "54322")
PGUSER = os.environ.get("SUPABASE_TEST_DB_USER", "postgres")
PGPASSWORD = os.environ.get("SUPABASE_TEST_DB_PASSWORD", "postgres")
PGDATABASE = os.environ.get("SUPABASE_TEST_DB_NAME", "postgres")

_PSQL_BIN = shutil.which("psql")

_WINDOW_INDEXES = {
    "idx_scores_pending_window": (
        "job_first_seen_at",
        "job_is_live AND (NOT excluded) AND (NOT is_graded)",
    ),
    "idx_scores_graded_window": (
        "recency_score",
        "job_is_live AND (NOT excluded) AND is_graded",
    ),
}


def _psql(query: str) -> str | None:
    """Run ``query`` via psql, returning stripped stdout, or ``None`` when the
    stack/psql isn't available (→ the caller skips). Same acknowledgement as
    ``test_privilege_invariants``: constant SQL, resolved absolute path.
    """
    if _PSQL_BIN is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — constant SQL, resolved psql path
            [
                _PSQL_BIN,
                "-h",
                PGHOST,
                "-p",
                PGPORT,
                "-U",
                PGUSER,
                "-d",
                PGDATABASE,
                "-t",
                "-A",
                "-q",
                "-c",
                query,
            ],
            env={**os.environ, "PGPASSWORD": PGPASSWORD},
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def _require_db() -> None:
    if _psql("SELECT 1") != "1":
        pytest.skip(
            "local Supabase DB not reachable via psql at "
            f"{PGHOST}:{PGPORT} — start the local stack to run this suite"
        )


@pytest.mark.parametrize("index_name", sorted(_WINDOW_INDEXES))
def test_window_index_exists_with_nulls_last(_require_db: None, index_name: str) -> None:
    indexdef = _psql(
        "SELECT indexdef FROM pg_indexes "  # noqa: S608 — constant-dict index_name
        f"WHERE schemaname = 'public' AND indexname = '{index_name}'"
    )
    assert indexdef, f"{index_name} missing — migration 20260905000000 not applied"
    order_col, _ = _WINDOW_INDEXES[index_name]
    assert f"{order_col} DESC NULLS LAST" in indexdef, indexdef
    assert "WHERE" in indexdef, f"{index_name} lost its partial predicate: {indexdef}"


@pytest.mark.parametrize("index_name", sorted(_WINDOW_INDEXES))
def test_window_index_serves_the_windows_order(_require_db: None, index_name: str) -> None:
    """EXPLAIN the tier's window shape: the plan must scan this index and
    contain NO sort node — index order == query order. Sabotage: rebuild the
    index without ``NULLS LAST`` and the Sort reappears here.
    """
    order_col, predicate = _WINDOW_INDEXES[index_name]
    graded = "true" if "AND is_graded" in predicate else "false"
    plan = _psql(
        "BEGIN; "  # noqa: S608 — every interpolated value is a module constant
        "SET LOCAL enable_seqscan = off; "
        "SET LOCAL enable_bitmapscan = off; "
        "SET LOCAL enable_sort = off; "
        "EXPLAIN (COSTS OFF) "
        "SELECT job_posting_id FROM public.scores "
        "WHERE target_id = '00000000-0000-0000-0000-000000000000' "
        f"AND excluded = false AND is_graded = {graded} AND job_is_live = true "
        f"ORDER BY {order_col} DESC NULLS LAST, job_posting_id DESC LIMIT 1000; "
        "ROLLBACK;"
    )
    assert plan, "EXPLAIN produced no output"
    assert index_name in plan, plan
    assert "Sort" not in plan, f"window shape still sorts — index order unusable:\n{plan}"
