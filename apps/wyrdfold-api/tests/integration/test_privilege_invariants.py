"""Privilege invariants for the reset local DB (Supabase audit 2026-07-02, MEDIUM).

`tests/test_migration_safety.py` is a *static* per-file guard (destructive DDL,
hot-index locks). It cannot see the *cumulative* privilege state — grants are the
sum of every migration's GRANT/REVOKE, and a later migration can widen or narrow
what an earlier one set. This suite pins two privilege invariants against the
ACTUAL reset database so grant drift can't recur silently:

  1. Every SECURITY DEFINER function in `public` is EXECUTE-able only by an
     explicit allowlist of roles — never by `anon`, and by `authenticated` only
     for the R2 hardened score-write RPCs whose bodies self-check `auth.uid()`.
  2. Designated service-role-only tables keep NO `anon`/`authenticated` table
     grants (they are reachable only via the service-role client, which bypasses
     RLS).

The whole point is to run against Postgres, so this is an `integration` test:
deselected by default, run via `pytest -m integration` (or the
`tests/integration/ -o addopts=""` invocation) with a live local stack. It
self-skips when `psql` is unavailable or the DB isn't reachable.

WHY THIS MATTERS: the repo has repeatedly had to repair grant drift after the
fact (20260620130000, 20260621120000, 20260629120000). Supabase's default
privileges grant EXECUTE to `anon`/`authenticated` on every NEW function and
`GRANT ALL` on new tables, so a fresh SECURITY DEFINER RPC or an internal table
is born too open unless a migration locks it down. This test fails the moment
that happens without an allowlist update, forcing the author to justify the
widening in review.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration

# Direct Postgres connection to the local Supabase DB (NOT the PostgREST port).
# The password/user are the well-known local-dev defaults; override via env for
# a non-default local setup or CI Postgres.
PGHOST = os.environ.get("SUPABASE_TEST_DB_HOST", "127.0.0.1")
PGPORT = os.environ.get("SUPABASE_TEST_DB_PORT", "54322")
PGUSER = os.environ.get("SUPABASE_TEST_DB_USER", "postgres")
PGPASSWORD = os.environ.get("SUPABASE_TEST_DB_PASSWORD", "postgres")
PGDATABASE = os.environ.get("SUPABASE_TEST_DB_NAME", "postgres")

# Absolute path to psql (or None → skip). Resolving it satisfies the "partial
# executable path" lint and means we never shell out to something off $PATH.
_PSQL_BIN = shutil.which("psql")

# --- Allowlists (the INTENTIONAL exceptions) --------------------------------

# SECURITY DEFINER functions in `public` that MAY be executed by
# `authenticated`. SEC-H2 (audit 2026-07-18): the #6 R2 score-write RPCs were
# the only entries; they are now service_role-only (a follower of a shared,
# ownerless target could otherwise vandalise its scores via direct PostgREST),
# so this allowlist is EMPTY — no public DEFINER function is
# authenticated-executable. NOTHING may be anon-executable either.
AUTHENTICATED_EXECUTABLE_DEFINER_FNS: frozenset[str] = frozenset()

# Tables documented + coded as service-role-only (no per-user owner column;
# reached only through the service-role client, which bypasses RLS). They must
# carry NO anon/authenticated table privileges.
SERVICE_ROLE_ONLY_TABLES: frozenset[str] = frozenset(
    {
        "job_embeddings",
        "prescan_shadow",
        # #467 §10 PR6 — the search-funnel ledger (identity-free by schema,
        # service-role buffered writes only; 20260725000000).
        "search_events",
    }
)


def _psql(query: str) -> str | None:
    """Run ``query`` via psql, returning stripped stdout, or ``None`` when the
    stack/psql isn't available (→ the caller skips).

    ``query`` is only ever the module's own constant SQL (no external/user
    input), and the executable is the absolute psql path resolved above — the
    S603 subprocess lint is acknowledged rather than a real injection surface.
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
                "-t",  # tuples only (no header/row-count footer)
                "-A",  # unaligned output
                "-q",  # quiet
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
            f"{PGHOST}:{PGPORT} — run `supabase start` (and install libpq/psql)"
        )


def test_no_definer_fn_is_anon_executable(_require_db: None) -> None:
    """No SECURITY DEFINER function in `public` may be callable by `anon`.

    An anon-executable DEFINER function runs with owner privileges for an
    unauthenticated caller — exactly the hole 20260629120000 closed on the
    score-write RPCs (an anon caller has `auth.uid() = NULL`, bypassing the
    in-body ownership guard).
    """
    rows = _psql(
        """
        SELECT p.proname
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prosecdef
          AND has_function_privilege('anon', p.oid, 'EXECUTE')
        ORDER BY p.proname;
        """
    )
    assert rows is not None
    offenders = [r for r in rows.splitlines() if r]
    assert not offenders, (
        "SECURITY DEFINER function(s) in public are anon-EXECUTE-able — a new "
        "function inherits Supabase's default anon EXECUTE grant and must "
        "REVOKE it (see 20260629120000):\n  - " + "\n  - ".join(offenders)
    )


def test_authenticated_definer_fns_are_allowlisted(_require_db: None) -> None:
    """Only the R2 hardened score-write RPCs may be `authenticated`-executable.

    Any other SECURITY DEFINER function reachable by `authenticated` is drift:
    either it was meant to be service-role-only (REVOKE authenticated) or it is a
    deliberate new user-facing RPC (add it here, with a body that self-checks
    `auth.uid()`).
    """
    rows = _psql(
        """
        SELECT p.proname
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prosecdef
          AND has_function_privilege('authenticated', p.oid, 'EXECUTE')
        ORDER BY p.proname;
        """
    )
    assert rows is not None
    executable = {r for r in rows.splitlines() if r}
    unexpected = executable - AUTHENTICATED_EXECUTABLE_DEFINER_FNS
    assert not unexpected, (
        "Unexpected authenticated-EXECUTE-able SECURITY DEFINER function(s). "
        "Either REVOKE `authenticated`, or add to "
        "AUTHENTICATED_EXECUTABLE_DEFINER_FNS with an auth.uid() self-check:\n"
        "  - " + "\n  - ".join(sorted(unexpected))
    )


def test_service_role_only_tables_have_no_user_grants(_require_db: None) -> None:
    """`job_embeddings` / `prescan_shadow` must carry no anon/authenticated grants.

    Pins 20260702170000: these internal tables are service-role-only. RLS
    (enabled + no policy) already denies user roles, but standing table grants
    are latent access the moment a policy is ever added — defense in depth.
    PUBLIC is included because a PUBLIC grant reaches anon/authenticated just
    the same — omitting it would let that drift slip past the guard.
    """
    # Pull every anon/authenticated/PUBLIC public-table grant and filter to our
    # set in Python — no SQL string interpolation (keeps it injection-free by
    # construction, not by trusting a constant).
    rows = _psql(
        """
        SELECT table_name || ':' || grantee || ':' || privilege_type
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND grantee IN ('anon', 'authenticated', 'PUBLIC')
        ORDER BY 1;
        """
    )
    assert rows is not None
    offenders = [
        r for r in rows.splitlines() if r and r.split(":", 1)[0] in SERVICE_ROLE_ONLY_TABLES
    ]
    assert not offenders, (
        "Service-role-only table(s) still grant privileges to "
        "anon/authenticated/PUBLIC — REVOKE them (see 20260702170000):\n  - "
        + "\n  - ".join(offenders)
    )


def test_score_rpcs_are_service_role_executable(_require_db: None) -> None:
    """SEC-H2: the score-write DEFINER RPCs are now service_role-only. Prove the
    query machinery finds them (so the empty-allowlist invariant test isn't
    pinning nothing) AND that they still exist as service_role-executable
    definer functions — i.e. the lockdown revoked `authenticated` without
    dropping/renaming them out from under the backend.
    """
    rows = _psql(
        """
        SELECT p.proname
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prosecdef
          AND has_function_privilege('service_role', p.oid, 'EXECUTE')
        ORDER BY p.proname;
        """
    )
    assert rows is not None
    executable = {r for r in rows.splitlines() if r}
    expected = {"user_upsert_score", "user_apply_score_blend", "user_set_scores_included"}
    assert expected.issubset(executable), (
        "Score-write RPCs must remain service_role-executable definer fns: "
        f"missing {sorted(expected - executable)}"
    )
