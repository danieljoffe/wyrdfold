"""Guard: destructive DDL in Supabase migrations must be deliberately marked.

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
