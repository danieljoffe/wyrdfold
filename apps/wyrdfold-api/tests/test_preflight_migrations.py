"""#1027: the release preflight that catches an unapplied migration.

Covers the pure logic — version parsing, pending-set arithmetic, identifier
validation. The database-touching parts (`psql`, `postgrest_probe`) are
exercised by running the script against a real database; what is pinned here is
everything that decides WHETHER a release is blocked, since a preflight that
silently computes an empty pending set is worse than no preflight at all.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preflight_migrations.py"
_spec = importlib.util.spec_from_file_location("preflight_migrations", _SCRIPT)
assert _spec and _spec.loader
preflight = importlib.util.module_from_spec(_spec)
sys.modules["preflight_migrations"] = preflight
_spec.loader.exec_module(preflight)


def _write(dirpath: Path, *names: str) -> None:
    for n in names:
        (dirpath / n).write_text("-- test migration\n")


# ---- version parsing -------------------------------------------------------


def test_parses_the_14_digit_version_prefix(tmp_path: Path):
    _write(tmp_path, "20260907000000_scores_exclusion_keywords.sql")
    assert preflight.repo_migrations(tmp_path) == {
        "20260907000000": "20260907000000_scores_exclusion_keywords.sql"
    }


def test_ignores_files_that_are_not_versioned_migrations(tmp_path: Path):
    """A stray .sql without a version prefix must not become a phantom pending
    migration — that would block every release until someone deleted it."""
    _write(tmp_path, "README.sql", "notes.sql", "2026_short.sql")
    assert preflight.repo_migrations(tmp_path) == {}


def test_ignores_non_sql_files(tmp_path: Path):
    _write(tmp_path, "20260907000000_real.sql")
    (tmp_path / "20260907000001_notes.md").write_text("x")
    assert list(preflight.repo_migrations(tmp_path)) == ["20260907000000"]


# ---- the decision: is a release blocked? -----------------------------------


def test_a_migration_missing_from_the_ledger_is_pending(tmp_path: Path):
    """The exact #1027 shape: present in the repo, absent from the database."""
    _write(tmp_path, "20260101000000_old.sql", "20260907000000_new.sql")
    repo = preflight.repo_migrations(tmp_path)
    applied = {"20260101000000"}
    pending = {v: n for v, n in repo.items() if v not in applied}
    assert pending == {"20260907000000": "20260907000000_new.sql"}, (
        "the unapplied migration must be reported, or the release ships code "
        "the database cannot serve"
    )


def test_a_fully_applied_repo_has_nothing_pending(tmp_path: Path):
    """The other half — the check has to be able to PASS, or it is just a wall."""
    _write(tmp_path, "20260101000000_a.sql", "20260907000000_b.sql")
    repo = preflight.repo_migrations(tmp_path)
    applied = {"20260101000000", "20260907000000"}
    assert {v: n for v, n in repo.items() if v not in applied} == {}


def test_a_ledger_entry_with_no_repo_file_is_not_a_blocker(tmp_path: Path):
    """Applied-but-not-in-repo is squashed/renamed history. It is reported as
    informational; treating it as blocking would wedge every release on a
    historical rename."""
    _write(tmp_path, "20260907000000_b.sql")
    repo = preflight.repo_migrations(tmp_path)
    applied = {"20250101000000_squashed"[:14], "20260907000000"}
    assert {v: n for v, n in repo.items() if v not in applied} == {}
    assert sorted(applied - set(repo)) == ["20250101000000"]


# ---- identifier validation (the SQL boundary) ------------------------------


@pytest.mark.parametrize("good", ["scores", "exclusion_keywords", "a", "t1_x"])
def test_accepts_plain_identifiers(good: str):
    assert preflight._ident(good, "probe") == good


@pytest.mark.parametrize(
    "hostile",
    [
        "scores; DROP TABLE scores--",
        "scores' OR '1'='1",
        'scores"',
        "Scores",  # uppercase: not a shape we emit, so not one we accept
        "",
        "1scores",
        "scores.x",
    ],
)
def test_rejects_anything_that_is_not_an_identifier(hostile: str):
    """These values reach SQL. #1016 shipped a version of exactly this mistake
    (a free-text date interpolated into a WHERE clause), so the boundary rejects
    rather than escapes."""
    with pytest.raises(SystemExit):
        preflight._ident(hostile, "probe")


# ---- fail-closed classification (review of #1028) --------------------------
#
# The first version returned True for every exception that was not PGRST204 —
# so an invalid key, a dead URL and a missing table all printed a green tick.
# Verified before fixing: an invalid service key and a refused connection both
# exited 0. These pin the classification so that cannot come back.


# The classification sets are asserted directly rather than through a fake
# client: the live behaviour was verified against a real database (invalid key
# -> PGRST301 exit 1; dead URL -> ConnectError exit 1; valid target -> both
# columns accepted), and a fake that agrees with the implementation would prove
# less than that did.


@pytest.mark.parametrize(
    "sqlstate",
    ["23502", "23503", "23505", "23514"],
)
def test_constraint_violations_prove_the_payload_was_accepted(sqlstate: str):
    """These can only be reached once PostgREST has parsed the payload and
    handed the row to Postgres — so they ARE proof the schema cache knows the
    columns, even though the write failed."""
    assert sqlstate in preflight._PAYLOAD_ACCEPTED_SQLSTATES


@pytest.mark.parametrize(
    "code",
    ["PGRST204", "PGRST205", "PGRST301", "PGRST302"],
)
def test_postgrest_rejections_are_never_proof(code: str):
    """Unknown column, unknown table, and both auth failures all mean the
    request never reached the table."""
    assert code in preflight._POSTGREST_REJECTED
    assert code not in preflight._PAYLOAD_ACCEPTED_SQLSTATES


def test_an_unrecognised_code_is_not_in_the_accepted_set():
    """The default must be 'not proof'. A timeout, a 500, a code added by a
    future PostgREST — none of them may pass silently."""
    for code in ("PGRST100", "42501", "08006", "", "None"):
        assert code not in preflight._PAYLOAD_ACCEPTED_SQLSTATES


# ---- target identity (review of #1028) ------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://abcdefghijklmnop.supabase.co", "abcdefghijklmnop"),
        (
            "postgresql://postgres.abcdefghijklmnop:pw@aws-1.pooler.supabase.com:5432/postgres",
            "abcdefghijklmnop",
        ),
        (
            "postgresql://postgres:pw@db.abcdefghijklmnop.supabase.co:5432/postgres",
            "abcdefghijklmnop",
        ),
        ("http://127.0.0.1:54321", "local"),
        ("postgresql://postgres:postgres@127.0.0.1:54322/postgres", "local"),
        ("http://localhost:54321", "local"),
    ],
)
def test_project_identity_recognises_both_url_shapes(url: str, expected: str):
    assert preflight._project_identity(url) == expected


def test_a_prod_ledger_and_a_local_probe_do_not_match():
    """The exact composite-green scenario: a production ledger paired with a
    local PostgREST would otherwise bless an environment that does not exist."""
    prod = preflight._project_identity(
        "postgresql://postgres.abcdefghijklmnop:pw@aws-1.pooler.supabase.com:5432/postgres"
    )
    local = preflight._project_identity("http://127.0.0.1:54321")
    assert prod != local


def test_an_unrecognised_host_never_reads_as_a_match():
    """Unknown must not collapse into some shared bucket that compares equal."""
    a = preflight._project_identity("https://something-else.example.com")
    b = preflight._project_identity("https://another.example.org")
    assert a != b
    assert a != "local"
    assert a.startswith("unknown:")


def test_redacted_url_never_leaks_credentials():
    out = preflight._redacted(
        "postgresql://postgres.abcdefghijklmnop:sup3rs3cr3t@aws-1.pooler.supabase.com:5432/postgres"
    )
    assert "sup3rs3cr3t" not in out
    assert "postgres.abcdefghijklmnop" not in out, "userinfo must not be printed"
    assert "aws-1.pooler.supabase.com:5432" in out


# ---- duplicate versions (review of #1028) ---------------------------------


def test_duplicate_migration_versions_are_fatal(tmp_path: Path):
    """Last-one-wins would hide a file from the ledger comparison, so a real
    unapplied migration could sit behind a green report."""
    _write(tmp_path, "20260907000000_a.sql", "20260907000000_b.sql")
    with pytest.raises(SystemExit):
        preflight.repo_migrations(tmp_path)
