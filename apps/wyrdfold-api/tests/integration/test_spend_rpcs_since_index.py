"""#1153: the ledger spend RPCs answer a dated window from the index, not a seq scan.

All five spend RPCs were ``LANGUAGE sql`` filtering with
``(p_since IS NULL OR created_at >= p_since)``. They carry ``SET search_path`` so
they are never inlined, and a non-inlined SQL function (PG 17) plans its
statement with ``p_since`` as an unknown parameter — a plan that must also
serve ``p_since IS NULL`` and so cannot use ``idx_llm_cost_log_created_at``.
Every call read the whole ledger (prod: ~11k buffers / 320-435 ms cold for a
one-day window that matched nothing; slowest calls at 7.3-7.9 s against the
8 s statement_timeout — and the guards fail closed since #1109). Migration
``20261027000000`` rewrites them in plpgsql, branched on ``p_since IS NULL``.

Three properties, each inside one rolled-back transaction on seeded rows:

* **No seq scan on a dated window** — ``pg_stat_xact_user_tables`` counts the
  scans the calls made. Plan-cache mode is left at its default (what prod
  runs) and each function is called past plpgsql's five-call custom-plan
  warm-up, so the plan the cache settles on is the one measured. Sabotage:
  the pre-#1153 definitions seq-scan on every call here.
* **Same answers** — each function equals the old predicate written inline, for
  both the all-time (NULL) and dated windows, per user, with and without
  excluded purposes.
* **The auth.uid() guard survives on both branches** — a JWT caller asking for
  another user's spend gets nothing (claims set without switching role, so RLS
  is not what is being tested).
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

_USER_A = "00000000-0000-4000-8000-00000000a153"
_USER_B = "00000000-0000-4000-8000-00000000b153"
_DATED = "(now() - interval '1 day')"
# Past plpgsql's five custom plans, so the cache has made its generic-vs-custom
# choice by the last calls.
_REPEATS = 8

# Two transient users; A gets 20k old filler rows (so a seq scan is never the
# cheap way to answer a recent window) plus a few recent rows; B a few of its own.
_SEED = f"""
BEGIN;
INSERT INTO auth.users (id, instance_id, aud, role, email) VALUES
  ('{_USER_A}', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   'spend-a@example.test'),
  ('{_USER_B}', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   'spend-b@example.test');
INSERT INTO public.llm_costs (user_id, model, purpose, cost_usd, created_at)
SELECT '{_USER_A}', 'm', 'p' || (i % 3), 0.001,
       now() - interval '40 days' - (i || ' minutes')::interval
FROM generate_series(1, 20000) i;
INSERT INTO public.llm_costs (user_id, model, purpose, cost_usd, created_at) VALUES
  ('{_USER_A}', 'm', 'p0', 0.25, now() - interval '1 hour'),
  ('{_USER_A}', 'm', 'p1', 0.50, now() - interval '2 hours'),
  ('{_USER_B}', 'm', 'p0', 1.00, now() - interval '3 hours'),
  ('{_USER_B}', 'm', 'p2', 2.00, now() - interval '30 days');
ANALYZE public.llm_costs;
"""  # noqa: S608 — module constants only

_SINCE_FILTER = "({since} IS NULL OR created_at >= {since})"

# name -> (function call with {since}, the old predicate written inline with {since})
_CALLS = {
    "total_spend_all_since": (
        "public.total_spend_all_since({since})",
        (
            "(SELECT COALESCE(SUM(cost_usd), 0)::numeric FROM public.llm_costs"  # noqa: S608
            f" WHERE {_SINCE_FILTER})"
        ),
    ),
    "total_spend_since": (
        f"public.total_spend_since('{_USER_A}', {{since}})",
        (
            "(SELECT COALESCE(SUM(cost_usd), 0)::numeric FROM public.llm_costs"  # noqa: S608
            f" WHERE user_id = '{_USER_A}' AND {_SINCE_FILTER})"
        ),
    ),
    "total_billable_spend_since": (
        f"public.total_billable_spend_since('{_USER_A}', {{since}}, ARRAY['p1'])",
        (
            "(SELECT COALESCE(SUM(cost_usd), 0)::numeric FROM public.llm_costs"  # noqa: S608
            f" WHERE user_id = '{_USER_A}' AND {_SINCE_FILTER}"
            " AND purpose <> ALL (ARRAY['p1']))"
        ),
    ),
    "total_billable_spend_since_no_exclusions": (
        f"public.total_billable_spend_since('{_USER_B}', {{since}}, NULL)",
        (
            "(SELECT COALESCE(SUM(cost_usd), 0)::numeric FROM public.llm_costs"  # noqa: S608
            f" WHERE user_id = '{_USER_B}' AND {_SINCE_FILTER})"
        ),
    ),
    "spend_by_purpose_since": (
        f"public.spend_by_purpose_since('{_USER_A}', {{since}})",
        (
            "(SELECT COALESCE(jsonb_object_agg(purpose, total), '{{}}'::jsonb) FROM ("  # noqa: S608
            " SELECT purpose, SUM(cost_usd)::numeric AS total FROM public.llm_costs"
            f" WHERE user_id = '{_USER_A}' AND {_SINCE_FILTER} GROUP BY purpose) g)"
        ),
    ),
    "cost_by_purpose_since": (
        f"public.cost_by_purpose_since('{_USER_B}', {{since}})",
        (
            "(SELECT COALESCE(jsonb_object_agg(purpose,"  # noqa: S608
            " jsonb_build_object('sum', total, 'count', n)), '{{}}'::jsonb) FROM ("
            " SELECT purpose, SUM(cost_usd)::numeric AS total, COUNT(*)::int AS n"
            f" FROM public.llm_costs WHERE user_id = '{_USER_B}'"
            f" AND {_SINCE_FILTER} GROUP BY purpose) g)"
        ),
    ),
}


def _psql_script(script: str) -> str | None:
    """Run ``script`` through psql's stdin (one session, every result printed),
    returning stripped stdout, or ``None`` when psql isn't installed (→ the
    caller skips). Constant SQL, resolved absolute path.
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
                "-X",
                "-t",
                "-A",
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            input=script,
            env={**os.environ, "PGPASSWORD": PGPASSWORD},
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        pytest.fail(f"psql failed:\n{proc.stderr}")
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def _require_db() -> None:
    try:
        reachable = _psql_script("SELECT 1;") == "1"
    except pytest.fail.Exception:
        reachable = False
    if not reachable:
        pytest.skip(
            "local Supabase DB not reachable via psql at "
            f"{PGHOST}:{PGPORT} — start the local stack to run this suite"
        )


@pytest.mark.parametrize("name", sorted(_CALLS))
def test_dated_window_does_not_seq_scan_the_ledger(_require_db: None, name: str) -> None:
    call = _CALLS[name][0].format(since=_DATED)
    out = _psql_script(
        _SEED  # noqa: S608 — module constants only
        + (
            "CREATE TEMP TABLE s0 ON COMMIT DROP AS SELECT seq_scan FROM pg_stat_xact_user_tables"  # noqa: S608
            " WHERE relid = 'public.llm_costs'::regclass;\n"
            f"SELECT {call} IS NOT NULL FROM generate_series(1, {_REPEATS});\n"
            "SELECT 'seq_scans=' || (x.seq_scan - s0.seq_scan) FROM pg_stat_xact_user_tables x, s0"
            " WHERE x.relid = 'public.llm_costs'::regclass;\n"
            "ROLLBACK;\n"
        )
    )
    if out is None:
        pytest.skip("psql not installed")
    assert out.splitlines()[-1] == "seq_scans=0", f"{name} seq-scanned llm_costs:\n{out}"


@pytest.mark.parametrize("since", ["NULL::timestamptz", _DATED])
@pytest.mark.parametrize("name", sorted(_CALLS))
def test_results_match_the_old_predicate(_require_db: None, name: str, since: str) -> None:
    call, expected = (s.format(since=since) for s in _CALLS[name])
    out = _psql_script(_SEED + f"SELECT ({call}) = {expected}, {call};\nROLLBACK;\n")
    if out is None:
        pytest.skip("psql not installed")
    matches, value = out.splitlines()[-1].split("|", 1)
    assert matches == "t", f"{name} at {since} diverged from the old predicate: {value}"
    # Vacuity guard: every seeded case has spend in both windows.
    assert value not in ("0", "0.000000", "{}"), f"{name} at {since} matched nothing: {value}"


@pytest.mark.parametrize("since", ["NULL::timestamptz", _DATED])
def test_jwt_caller_cannot_read_another_users_spend(_require_db: None, since: str) -> None:
    claims = f'{{"sub": "{_USER_B}", "role": "authenticated"}}'
    out = _psql_script(
        _SEED
        + (
            f"SELECT set_config('request.jwt.claims', '{claims}', true);\n"
            f"SELECT public.total_spend_since('{_USER_A}', {since}) || '|' ||"
            f" public.total_billable_spend_since('{_USER_A}', {since}, NULL) || '|' ||"
            f" public.spend_by_purpose_since('{_USER_A}', {since}) || '|' ||"
            f" public.cost_by_purpose_since('{_USER_A}', {since}) || '|' ||"
            # Own spend is still visible — the guard is not simply returning zero.
            f" public.total_spend_since('{_USER_B}', {since});\n"
            "ROLLBACK;\n"
        )
    )
    if out is None:
        pytest.skip("psql not installed")
    *others, own = out.splitlines()[-1].split("|")
    assert [float(others[0]), float(others[1]), others[2], others[3]] == [0.0, 0.0, "{}", "{}"], out
    assert float(own) > 0, out
