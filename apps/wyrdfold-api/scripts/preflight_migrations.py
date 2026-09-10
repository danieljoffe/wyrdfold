"""Release preflight: is the target database migrated for the code being deployed?

WHY THIS EXISTS
Migrations are applied by hand (``.claude/rules/api-validation.md``), and nothing
detects the one ordering that is fatal: **deployed code that requires a migration
the database does not have.** The failure is quiet where it matters — the API
boots fine, passes its healthcheck, and only fails later at the first write, deep
inside a poll cycle, as a logged exception. The deploy looks successful.

Release #1027 hit exactly this: `20260907000000_scores_exclusion_keywords.sql`
sat unapplied while the API in the release writes both of its columns
unconditionally, so every scoring upsert would have failed with ``PGRST204``. It
was caught by reading the diff, not by any check. This is that check.

It is also the class in ``docs/decisions.md`` → "RLS policies never reached
prod": a migration that lives in the repo and never reaches the database.

WHAT IT PROVES
1. Every migration in ``supabase/migrations/`` appears in the target's ledger.
2. Optionally (``--probe-table``), that PostgREST **accepts writes to the new
   columns** — the schema *cache*, not just the SQL catalog. A column can exist
   in ``information_schema`` while PostgREST still rejects it, and PostgREST is
   how this application writes. That gap is why a catalog query alone is not
   sufficient evidence.

READ-ONLY by default. ``--probe-table`` performs ONE insert and deletes it in a
``finally``; it touches no pre-existing row.

    cd apps/wyrdfold-api
    export DATABASE_URL=...            # ledger check (read-only)
    uv run python scripts/preflight_migrations.py
    # add the PostgREST proof:
    uv run python scripts/preflight_migrations.py \
        --probe-table scores --probe-column exclusion_keywords

Exit 0 = safe to deploy. Non-zero = do NOT merge; the deploy would ship code
the database cannot serve.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# Identifiers reaching SQL must be identifier-shaped, not escaped. Same lesson as
# #1016's --cap-deployed: the safe move is to reject anything that is not the
# thing you expect, at the boundary, rather than to quote it and hope.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _ident(value: str, what: str) -> str:
    """A lowercase SQL identifier, or exit. Never quoted-and-hoped."""
    if not _IDENT_RE.match(value):
        sys.exit(f"{what} must be a plain lowercase identifier, got {value!r}")
    return value


# Supabase's own ledger. Same table the CLI stamps, so a migration applied by
# any route (CLI, MCP apply_migration, psql + manual stamp) is visible here.
LEDGER = "supabase_migrations.schema_migrations"
LEDGER_QUERY = "SELECT version FROM supabase_migrations.schema_migrations;"

# ``20260907000000_scores_exclusion_keywords.sql`` -> ``20260907000000``
_VERSION_RE = re.compile(r"^(\d{14})_")


def repo_migrations(migrations_dir: Path) -> dict[str, str]:
    """version -> filename, for every .sql in the migrations directory."""
    found: dict[str, str] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _VERSION_RE.match(path.name)
        if m:
            found[m.group(1)] = path.name
    return found


def psql(sql: str, **params: str) -> list[str]:
    """One query against DATABASE_URL. Mirrors the access pattern
    ``scripts/model_phase1_economics.py`` uses — psql, not PostgREST, because the
    ledger lives in a schema PostgREST does not expose.

    ``params`` bind through psql's own ``-v`` / ``:'name'`` quoting, so no caller
    interpolates a value into SQL text. The identifiers are ALSO validated by
    ``_ident`` at the CLI boundary; belt and braces, because #1016 shipped a
    version of exactly this mistake.
    """
    binary = shutil.which("psql") or "/opt/homebrew/opt/libpq/bin/psql"
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set (it lives in apps/wyrdfold-api/.env.local)")
    bindings: list[str] = []
    for name, value in params.items():
        bindings += ["-v", f"{name}={value}"]
    # SQL goes over STDIN, not -c. psql only performs :'var' substitution on
    # input it reads as a script; inside -c the ":" reaches the server verbatim
    # and errors. Verified both ways before relying on it.
    proc = subprocess.run(  # noqa: S603 — constant SQL, resolved binary
        [binary, url, "-X", "-q", "-A", "-t", *bindings],
        input=sql,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        sys.exit(f"psql failed: {proc.stderr.strip()[:300]}")
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def column_facts(table: str, prefix: str) -> list[tuple[str, str, str, str]]:
    """(name, type, is_nullable, default) for columns matching ``prefix``.

    Reported so the operator can eyeball the shape the migration promised —
    nullable, no default — rather than only that a column exists. A NOT NULL
    DEFAULT would silently change what an absent value MEANS.
    """
    table = _ident(table, "--show-columns table")
    prefix = _ident(prefix, "--show-columns prefix")
    rows = psql(
        "SELECT column_name || '|' || data_type || '|' || is_nullable || '|' "
        "|| COALESCE(column_default, '(none)') "
        "FROM information_schema.columns WHERE table_name = :'tbl' "
        "AND column_name LIKE :'pfx' || '%' ORDER BY column_name;",
        tbl=table,
        pfx=prefix,
    )
    out = []
    for r in rows:
        parts = r.split("|")
        if len(parts) == 4:
            out.append((parts[0], parts[1], parts[2], parts[3]))
    return out


def postgrest_probe(table: str, column: str) -> tuple[bool, str]:
    """Write a throwaway row through PostgREST touching ``column``, then delete it.

    The point is the SCHEMA CACHE. PostgREST serves writes from a cached schema
    and rejects unknown columns with ``PGRST204`` even when the column exists in
    the catalog — so ``information_schema`` agreeing is not evidence that the
    application can write. This is the only check that exercises the same path
    the API uses.
    """
    try:
        from supabase import create_client
    except ImportError:
        return False, "supabase client not importable — run inside the api venv"

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        return False, "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set"

    table = _ident(table, "--probe-table")
    column = _ident(column, "--probe-column")
    sb = create_client(url, key)
    probe_id = str(uuid.uuid4())
    try:
        # A column-only probe: send the id and the column under test. If the
        # table needs more NOT NULL columns this errors on THOSE, which is a
        # legible failure (and never a false pass).
        sb.table(table).insert({"id": probe_id, column: None}).execute()
        return True, f"PostgREST accepted a write to {table}.{column}"
    except Exception as exc:  # the message IS the result
        msg = str(exc)
        if "PGRST204" in msg:
            return False, f"PGRST204 — PostgREST does not know {table}.{column}"
        # Any other error means the column was accepted and something else
        # (a NOT NULL sibling, an FK) rejected the row — which still proves
        # the schema cache knows the column.
        return True, f"column accepted; row rejected for an unrelated reason: {msg[:90]}"
    finally:
        with contextlib.suppress(Exception):
            sb.table(table).delete().eq("id", probe_id).execute()


def main() -> int:
    ap = argparse.ArgumentParser(description="Release preflight: is the DB migrated?")
    ap.add_argument(
        "--migrations-dir",
        default=str(Path(__file__).resolve().parents[3] / "supabase" / "migrations"),
    )
    ap.add_argument("--probe-table", help="table to prove PostgREST accepts writes to")
    ap.add_argument("--probe-column", help="column on --probe-table to write")
    ap.add_argument(
        "--show-columns",
        metavar="TABLE:PREFIX",
        help="report type/nullability/default for columns matching a prefix, "
        "e.g. scores:exclusion_keywords",
    )
    a = ap.parse_args()

    migrations_dir = Path(a.migrations_dir)
    if not migrations_dir.is_dir():
        sys.exit(f"migrations dir not found: {migrations_dir}")

    repo = repo_migrations(migrations_dir)
    applied = set(psql(LEDGER_QUERY))
    pending = {v: n for v, n in repo.items() if v not in applied}
    # Applied-but-absent-from-repo: usually a squashed/renamed history, not a
    # blocker, but worth showing — it is the signature of a ledger and a repo
    # that have drifted apart.
    orphaned = sorted(applied - set(repo))

    print("MIGRATION PREFLIGHT")
    print(f"  migrations dir      : {migrations_dir}")
    print(f"  in repo             : {len(repo)}")
    print(f"  applied in ledger   : {len(applied)}")
    print(f"  PENDING (unapplied) : {len(pending)}")
    if orphaned:
        print(f"  in ledger, not repo : {len(orphaned)} (informational)")

    if pending:
        print("\n  ⛔ NOT SAFE TO DEPLOY — these are in the code but not the database:")
        for v in sorted(pending):
            print(f"     {v}  {pending[v]}")
        print(
            "\n  Deploying now ships code the database cannot serve. If the new\n"
            "  code writes any of these columns unconditionally, EVERY such write\n"
            "  fails with PGRST204 — while the API still boots and reports healthy."
        )
    else:
        print("\n  ✅ every repo migration is present in the ledger")

    if a.show_columns:
        table, _, prefix = a.show_columns.partition(":")
        facts = column_facts(table, prefix)
        print(f"\nCOLUMN SHAPE — {table}.{prefix}*")
        if not facts:
            print("  (none found)")
        for name, typ, nullable, default in facts:
            print(f"  {name}: {typ}, nullable={nullable}, default={default}")

    probe_failed = False
    if a.probe_table and a.probe_column:
        ok, detail = postgrest_probe(a.probe_table, a.probe_column)
        print("\nPOSTGREST SCHEMA-CACHE PROBE")
        print(f"  {'✅' if ok else '⛔'} {detail}")
        if not ok:
            probe_failed = True
            print(
                "  The catalog and the schema cache disagree, or the column is\n"
                "  missing. The API writes through PostgREST, so this is the check\n"
                "  that matters — a passing information_schema query is not enough."
            )

    return 1 if (pending or probe_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
