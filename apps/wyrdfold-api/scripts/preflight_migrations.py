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

    # the full #1027 gate — ledger + column shapes + a real PostgREST write
    # carrying BOTH columns in one payload:
    export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=...
    uv run python scripts/preflight_migrations.py \
        --show-columns scores:exclusion_keywords \
        --probe-table scores \
        --probe-columns exclusion_keywords,exclusion_keywords_version

DATABASE_URL and SUPABASE_URL are independent variables, so the script derives a
project identity from each and refuses to report on a split target — a prod
ledger blessed by a local PostgREST would otherwise produce a green describing
no environment that exists.

Exit 0 = safe to deploy. Non-zero = do NOT merge; the deploy would ship code
the database cannot serve. Every uncertain outcome is non-zero: this is a gate,
so absence of evidence is never evidence.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _project_identity(raw: str) -> str:
    """A comparable identity for a Supabase target, from a URL of either shape.

    The ledger check reads ``DATABASE_URL`` and the schema probe reads
    ``SUPABASE_URL`` — two independent variables that nothing forces to agree.
    A prod ledger paired with a local PostgREST would produce a composite green
    describing no environment that exists (review of #1028), which is the worst
    possible output from a release gate.

    Recognised shapes:
      https://<ref>.supabase.co                      -> <ref>
      postgresql://postgres.<ref>:pw@...pooler...    -> <ref>   (pooler)
      postgresql://...@db.<ref>.supabase.co/...      -> <ref>   (direct)
      anything on localhost / 127.0.0.1              -> "local"
    Unrecognised shapes return "unknown:<host>" so they compare unequal to
    everything except an identical host — unknown must never read as a match.
    """
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
        return "local"
    m = re.match(r"^([a-z0-9]{16,})\.supabase\.co$", host)
    if m:
        return m.group(1)
    m = re.match(r"^db\.([a-z0-9]{16,})\.supabase\.co$", host)
    if m:
        return m.group(1)
    # Pooler: the project ref rides in the USERNAME as postgres.<ref>.
    user = parsed.username or ""
    m = re.match(r"^postgres\.([a-z0-9]{16,})$", user)
    if m:
        return m.group(1)
    return f"unknown:{host}"


def _redacted(raw: str) -> str:
    """A URL safe to print: scheme, host, port. Never userinfo, never a key."""
    parsed = urlparse(raw)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def repo_migrations(migrations_dir: Path) -> dict[str, str]:
    """version -> filename, for every .sql in the migrations directory.

    Duplicate versions are fatal rather than last-one-wins: two files sharing a
    version means one of them is invisible to the ledger comparison, so a real
    migration could sit unapplied while this reports all-clear (review of
    #1028).
    """
    found: dict[str, str] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _VERSION_RE.match(path.name)
        if not m:
            continue
        version = m.group(1)
        if version in found:
            sys.exit(
                f"duplicate migration version {version}: {found[version]} and "
                f"{path.name}. One would be hidden from the ledger comparison."
            )
        found[version] = path.name
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


# SQLSTATEs that can only be reached AFTER PostgREST has parsed the payload and
# handed the row to Postgres. Their occurrence is therefore positive evidence
# that the schema cache knows every column we sent. Everything else — auth,
# connectivity, unknown-column, unknown-relation, timeouts, anything
# unrecognised — is NOT evidence and must fail closed.
_PAYLOAD_ACCEPTED_SQLSTATES = {
    "23502",  # not_null_violation — a sibling column we deliberately omitted
    "23503",  # foreign_key_violation — the ids are synthetic
    "23505",  # unique_violation
    "23514",  # check_violation
}

# PostgREST's own codes, all of which mean the request never reached the table.
_POSTGREST_REJECTED = {
    "PGRST204": "PostgREST does not know one of these columns (schema cache)",
    "PGRST205": "PostgREST does not know this table",
    "PGRST301": "authentication failed (bad or expired key)",
    "PGRST302": "authentication required",
}


def postgrest_probe(table: str, columns: list[str]) -> tuple[bool, str]:
    """Write a throwaway row through PostgREST carrying EVERY column in
    ``columns``, then delete it. Returns ``(proved, detail)``.

    THE POINT IS THE SCHEMA CACHE. PostgREST serves writes from a cached schema
    and answers ``PGRST204`` for a column it has not picked up, even when
    ``information_schema`` already has it. The API writes through PostgREST, so
    this is the only check that exercises the path a deploy depends on.

    FAILS CLOSED. Only two outcomes count as proof: the insert succeeded, or
    Postgres rejected the row with an integrity-constraint SQLSTATE, which can
    only happen once PostgREST has already accepted the payload shape.

    An earlier version treated *every* non-``PGRST204`` exception as success.
    Review of #1028 caught it, and it was as bad as it sounds — verified before
    fixing: an invalid service key (``PGRST301``) and a URL with nothing
    listening (``Connection refused``) both printed a green tick and exited 0.
    A gate that certifies an unreachable database is worse than no gate, because
    it launders the absence of evidence into evidence.

    ALL columns go in ONE payload. #1027 needs proof for both
    ``exclusion_keywords`` and ``exclusion_keywords_version``, because the API
    emits both unconditionally — proving one and inferring the other from the
    SQL catalog is not the same claim.
    """
    try:
        from postgrest.exceptions import APIError
        from supabase import create_client
    except ImportError:
        return False, "supabase/postgrest client not importable — run inside the api venv"

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        return False, "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set"

    table = _ident(table, "--probe-table")
    cols = [_ident(c, "--probe-column") for c in columns]
    sb = create_client(url, key)
    probe_id = str(uuid.uuid4())
    payload: dict[str, Any] = {"id": probe_id}
    for c in cols:
        payload[c] = None

    inserted = False
    try:
        sb.table(table).insert(payload).execute()
        inserted = True
        proved, detail = True, f"PostgREST accepted a write carrying {', '.join(cols)}"
    except APIError as exc:
        code = str(exc.code) if exc.code else "(no code)"
        if code in _PAYLOAD_ACCEPTED_SQLSTATES:
            proved = True
            detail = (
                f"all of {', '.join(cols)} accepted; the ROW was then rejected by "
                f"the database ({code}) — which only happens after PostgREST "
                "accepted the payload"
            )
        elif code in _POSTGREST_REJECTED:
            proved = False
            detail = f"{code} — {_POSTGREST_REJECTED[code]}"
        else:
            # Unrecognised: NOT proof. Naming the code keeps this debuggable
            # without ever letting an unknown outcome pass as success.
            proved = False
            detail = f"unrecognised API error {code} — not proof of schema-cache acceptance"
    except Exception as exc:
        # Network, DNS, TLS, timeout, anything else. Never proof.
        proved = False
        detail = f"{type(exc).__name__}: {str(exc)[:90]} — not proof of schema-cache acceptance"

    if inserted:
        # A cleanup failure must be LOUD. Silently leaving a probe row behind
        # while reporting success would make this tool a source of the drift it
        # is supposed to detect (review of #1028).
        try:
            sb.table(table).delete().eq("id", probe_id).execute()
        except Exception as exc:
            return False, (
                f"probe row {probe_id} was INSERTED but could not be deleted "
                f"({type(exc).__name__}: {str(exc)[:70]}). Remove it by hand."
            )
    return proved, detail


def main() -> int:
    ap = argparse.ArgumentParser(description="Release preflight: is the DB migrated?")
    ap.add_argument(
        "--migrations-dir",
        default=str(Path(__file__).resolve().parents[3] / "supabase" / "migrations"),
    )
    ap.add_argument("--probe-table", help="table to prove PostgREST accepts writes to")
    ap.add_argument(
        "--probe-columns",
        help="comma-separated columns sent in ONE payload, e.g. "
        "exclusion_keywords,exclusion_keywords_version. Proving one and "
        "inferring the rest from the SQL catalog is a different, weaker claim",
    )
    ap.add_argument(
        "--allow-target-mismatch",
        action="store_true",
        help="proceed even when DATABASE_URL and SUPABASE_URL name different "
        "projects. Off by default: a mixed pair yields a green that describes "
        "no real environment",
    )
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

    # Identity FIRST: everything below describes a target, and a split target
    # makes the whole report meaningless (review of #1028).
    db_url = os.environ.get("DATABASE_URL", "")
    rest_url = os.environ.get("SUPABASE_URL", "")
    db_id = _project_identity(db_url) if db_url else "(DATABASE_URL unset)"
    rest_id = _project_identity(rest_url) if rest_url else "(SUPABASE_URL unset)"

    print("TARGET IDENTITY")
    print(f"  ledger  (DATABASE_URL) : {_redacted(db_url) if db_url else '(unset)'}  -> {db_id}")
    if rest_url:
        print(f"  schema  (SUPABASE_URL) : {_redacted(rest_url)}  -> {rest_id}")
    mismatch = bool(rest_url and db_url and db_id != rest_id)
    if mismatch:
        print(
            f"\n  ⛔ TARGET MISMATCH: the ledger says {db_id!r} and the schema probe\n"
            f"  says {rest_id!r}. A pass here would describe no real environment —\n"
            "  a production ledger blessed by a local PostgREST, for instance.\n"
            "  Fix the environment, or pass --allow-target-mismatch deliberately."
        )
        if not a.allow_target_mismatch:
            return 1
        print("  (continuing under --allow-target-mismatch)")

    print()
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
    if a.probe_table and a.probe_columns:
        cols = [c.strip() for c in a.probe_columns.split(",") if c.strip()]
        ok, detail = postgrest_probe(a.probe_table, cols)
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
