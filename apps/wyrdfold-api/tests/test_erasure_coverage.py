"""Erasure-coverage guard (#29).

Every ``public`` base table with a ``user_id`` *column* must be consciously
classified in ``account_deletion`` — deleted (``_USER_ID_TABLES``),
anonymized (``_ANONYMIZED_TABLES``), or the profile row. We scan the
migration DDL (the schema source of truth, incl. for self-hosters) rather
than a live DB so this runs in the standard pytest job, and so a NEW per-user
table can't silently slip account deletion — the gap that left
``reference_jds`` + ``contribution_votes`` behind until this guard.

The scan errs toward over-inclusion (a column named ``user_id`` typed
``uuid``/``text``), which fails loud and is resolved by classifying the table;
it cannot under-include a real per-user table, which is the dangerous miss.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.account_deletion import ERASURE_HANDLED_USER_ID_TABLES

# A ``user_id`` *column* def — ``user_id`` (optionally quoted) followed by its
# type. Anchored on the type so a foreign-key reference like
# ``REFERENCES user_profiles("user_id")`` (``user_id`` followed by ``)``) does
# NOT match. Every ``user_id`` in this schema is ``uuid`` or ``text``.
_USER_ID_COLUMN = re.compile(r'"?user_id"?\s+"?(?:uuid|text)\b', re.IGNORECASE)

_CREATE_TABLE = re.compile(
    r'create\s+table\s+(?:if\s+not\s+exists\s+)?"?(?:public"?\.)?"?(\w+)"?\s*\(',
    re.IGNORECASE,
)
_ALTER_ADD_USER_ID = re.compile(
    r'alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?"?(?:public"?\.)?"?(\w+)"?\s+'
    r'add\s+column\s+(?:if\s+not\s+exists\s+)?"?user_id"?\s+"?(?:uuid|text)\b',
    re.IGNORECASE,
)


def _migrations_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "supabase" / "migrations"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("supabase/migrations directory not found")


def _balanced_body(text: str, open_paren_idx: int) -> str:
    """Return the text between ``text[open_paren_idx]`` ('(') and its match."""
    depth = 0
    for i in range(open_paren_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_idx + 1 : i]
    return text[open_paren_idx + 1 :]


def _tables_with_user_id_column() -> set[str]:
    found: set[str] = set()
    for sql_file in _migrations_dir().glob("*.sql"):
        text = sql_file.read_text(encoding="utf-8")
        for match in _CREATE_TABLE.finditer(text):
            body = _balanced_body(text, match.end() - 1)
            if _USER_ID_COLUMN.search(body):
                found.add(match.group(1))
        for match in _ALTER_ADD_USER_ID.finditer(text):
            found.add(match.group(1))
    return found


def test_every_user_id_table_is_classified_for_erasure() -> None:
    schema_tables = _tables_with_user_id_column()
    # Sanity: the scan must actually find tables (a broken regex finding none
    # would make the guard vacuously pass).
    assert "user_profiles" in schema_tables, schema_tables
    assert "reference_jds" in schema_tables, schema_tables

    unhandled = schema_tables - ERASURE_HANDLED_USER_ID_TABLES
    assert not unhandled, (
        "These tables have a `user_id` column but are not classified for "
        f"account erasure: {sorted(unhandled)}. Add each to "
        "account_deletion._USER_ID_TABLES (delete) or _ANONYMIZED_TABLES "
        "(null the link), per #29."
    )
