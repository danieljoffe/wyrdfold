"""Guards for Supabase migration safety (run in CI, fail on a bad new migration).

Two guards live here:
- Destructive DDL must be deliberately marked (#109) — below.
- Index builds on hot tables must be CONCURRENTLY or acknowledged (#112) —
  see the second section.

Issue #109. A ``DROP COLUMN`` / ``DROP TABLE`` / ``TRUNCATE`` is irreversible —
if the backfill it depends on is wrong, the row data is gone with no recovery
path (the retrospective that prompted this: the c4 ``jobs.status`` drop). This
test fails CI when a *new* migration introduces a destructive statement without
an explicit guard marker, forcing the author to follow the snapshot-and-verify
convention (CONTRIBUTING.md -> "Database migrations") and a reviewer to see the
deliberate acknowledgement.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/ -> wyrdfold-api -> apps -> repo root -> supabase/migrations
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "supabase" / "migrations"

# Author adds this (in a comment) on the same migration as the destructive op,
# once they've followed the snapshot-and-verify convention in CONTRIBUTING.md.
GUARD_MARKER = "guarded-destructive:"

# Migrations that predate this guard and are already applied. Migrations are
# forward-only (never edited after merge), so these can't retroactively carry
# the marker. NEW destructive migrations must use the marker, not this list.
GRANDFATHERED: frozenset[str] = frozenset({"20260614170000_c4_drop_jobs_status.sql"})

# Irreversible, row-data-destroying DDL. DROP CONSTRAINT / DROP INDEX keep the
# row data, so they're intentionally out of scope.
_DESTRUCTIVE = re.compile(r"\b(drop\s+column|drop\s+table|truncate)\b", re.IGNORECASE)

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` blocks so a destructive verb
    mentioned only in prose (e.g. "...gets TRUNCATED...") doesn't trip the check.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", sql))


def _migration_files() -> list[Path]:
    assert MIGRATIONS_DIR.is_dir(), f"migrations dir not found: {MIGRATIONS_DIR}"
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def test_destructive_migrations_are_guarded() -> None:
    offenders: list[str] = []
    for path in _migration_files():
        raw = path.read_text(encoding="utf-8")
        if not _DESTRUCTIVE.search(_strip_sql_comments(raw)):
            continue
        if path.name in GRANDFATHERED:
            continue
        if GUARD_MARKER in raw:  # the marker lives in a comment
            continue
        offenders.append(path.name)

    listing = "\n".join(f"  - {name}" for name in offenders)
    msg = (
        "Unguarded destructive DDL (DROP COLUMN / DROP TABLE / TRUNCATE) "
        "found in:\n"
        f"{listing}\n\n"
        "These operations are irreversible. Follow the snapshot-and-verify\n"
        "convention (CONTRIBUTING.md -> 'Database migrations'), then add a\n"
        f"`-- {GUARD_MARKER} <why safe / where the snapshot is>` comment to the\n"
        "migration acknowledging the guard."
    )
    assert not offenders, msg


def test_grandfathered_list_has_no_stale_entries() -> None:
    """An allowlist that silently rots is worse than none — every grandfathered
    name must still exist on disk.
    """
    names = {path.name for path in _migration_files()}
    missing = sorted(GRANDFATHERED - names)
    assert not missing, f"GRANDFATHERED lists migrations that no longer exist: {missing}"


def test_detector_flags_destructive_and_ignores_comments() -> None:
    # Real destructive DDL is caught.
    assert _DESTRUCTIVE.search(_strip_sql_comments("ALTER TABLE t DROP COLUMN c;"))
    assert _DESTRUCTIVE.search(_strip_sql_comments("DROP TABLE t;"))
    assert _DESTRUCTIVE.search(_strip_sql_comments("TRUNCATE t;"))
    # Comment-only mentions are ignored.
    assert not _DESTRUCTIVE.search(
        _strip_sql_comments("-- this could TRUNCATE the tail\nSELECT 1;")
    )
    assert not _DESTRUCTIVE.search(
        _strip_sql_comments("/* DROP COLUMN note */ SELECT 1;")
    )
    # Additive DDL is fine.
    assert not _DESTRUCTIVE.search(_strip_sql_comments("ALTER TABLE t ADD COLUMN c int;"))


# ---------------------------------------------------------------------------
# Index-lock guard (#112). A plain `CREATE INDEX` takes a SHARE lock that
# blocks writes to the table for the build duration. On hot, continuously
# written tables that stalls the poller / scoring / cost writers. A new index
# on such a table must either build `CONCURRENTLY` (in its own single-statement
# migration — `CONCURRENTLY` can't run inside the txn `supabase db push` wraps
# each file in) or carry an `-- index-lock-ok:` comment acknowledging that a
# brief write lock is acceptable at the current (small) scale.
# ---------------------------------------------------------------------------

INDEX_LOCK_MARKER = "index-lock-ok:"

# Continuously written / growing tables where a build-time write lock hurts.
HOT_TABLES: frozenset[str] = frozenset(
    {"jobs", "scores", "llm_costs", "status_log", "source_discoveries", "user_jobs"}
)

# Migrations that predate this guard and are already applied (forward-only,
# never edited post-merge). New migrations must comply, not extend this list.
INDEX_GRANDFATHERED: frozenset[str] = frozenset(
    {
        "20260612015641_remote_schema.sql",
        "20260614170000_c4_drop_jobs_status.sql",
        "20260614180000_perf_indexes.sql",
        "20260615000000_user_jobs_job_posting_idx.sql",
    }
)

# CREATE [UNIQUE] INDEX [CONCURRENTLY] [IF NOT EXISTS] <name> ON [public.]<table>
_CREATE_INDEX = re.compile(
    r"create\s+(?:unique\s+)?index\s+(concurrently\s+)?(?:if\s+not\s+exists\s+)?"
    r'"?[a-z0-9_]+"?\s+on\s+(?:"?public"?\.)?"?([a-z0-9_]+)"?',
    re.IGNORECASE,
)


def _plain_hot_index_tables(sql_without_comments: str) -> list[str]:
    """Hot tables that get a NON-concurrent CREATE INDEX in this SQL."""
    found: list[str] = []
    for match in _CREATE_INDEX.finditer(sql_without_comments):
        is_concurrent = bool(match.group(1))
        table = match.group(2).lower()
        if table in HOT_TABLES and not is_concurrent:
            found.append(table)
    return found


def test_new_index_on_hot_table_is_concurrent_or_marked() -> None:
    offenders: list[str] = []
    for path in _migration_files():
        if path.name in INDEX_GRANDFATHERED:
            continue
        raw = path.read_text(encoding="utf-8")
        if INDEX_LOCK_MARKER in raw:  # author consciously accepted the lock
            continue
        for table in _plain_hot_index_tables(_strip_sql_comments(raw)):
            offenders.append(f"{path.name}: plain CREATE INDEX on {table!r}")

    listing = "\n".join(f"  - {o}" for o in offenders)
    msg = (
        "Plain CREATE INDEX on a hot, continuously-written table blocks writes "
        "for the whole build:\n"
        f"{listing}\n\n"
        "Either build it CONCURRENTLY in its own single-statement migration\n"
        "(CONCURRENTLY can't run inside the txn `supabase db push` wraps each\n"
        "file in), or — if the table is still small enough that a brief write\n"
        f"lock is fine — add a `-- {INDEX_LOCK_MARKER} <reason>` comment to the\n"
        "migration. See CONTRIBUTING.md -> 'Database migrations'."
    )
    assert not offenders, msg


def test_index_grandfather_list_has_no_stale_entries() -> None:
    names = {path.name for path in _migration_files()}
    missing = sorted(INDEX_GRANDFATHERED - names)
    assert not missing, (
        f"INDEX_GRANDFATHERED lists migrations that no longer exist: {missing}"
    )


def test_index_detector_flags_plain_hot_and_ignores_concurrent() -> None:
    # Plain index on a hot table is flagged.
    assert _plain_hot_index_tables("CREATE INDEX foo ON public.jobs (x);") == ["jobs"]
    assert _plain_hot_index_tables(
        'CREATE INDEX "i" ON "public"."llm_costs" (x);'
    ) == ["llm_costs"]
    # CONCURRENTLY is allowed.
    assert (
        _plain_hot_index_tables("CREATE INDEX CONCURRENTLY foo ON public.jobs (x);")
        == []
    )
    # Cold tables are not guarded (small-table builds are fine, per #112).
    assert _plain_hot_index_tables("CREATE INDEX foo ON public.user_profiles (x);") == []
    # Comment-only mentions don't count (comments are stripped first).
    assert (
        _plain_hot_index_tables(
            _strip_sql_comments("-- CREATE INDEX foo ON jobs (x)\nSELECT 1;")
        )
        == []
    )
