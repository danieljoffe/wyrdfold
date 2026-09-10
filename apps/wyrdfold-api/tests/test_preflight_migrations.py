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


# ---- postgrest_probe control flow, through a fake client -------------------
#
# Review of #1028, round 2: the earlier tests only asserted membership in the
# accepted/rejected constants, so a regression that swapped the branches,
# ignored ``exc.code`` or swallowed a cleanup failure would have stayed green.
# These drive the real function and assert the verdict it returns.


class _FakeQuery:
    def __init__(self, owner: _FakeClient, op: str) -> None:
        self._owner, self._op = owner, op

    def eq(self, *_a: object, **_k: object) -> _FakeQuery:
        return self

    def execute(self) -> object:
        if self._op == "insert":
            if self._owner.insert_error is not None:
                raise self._owner.insert_error
            self._owner.inserted = True
            return object()
        self._owner.delete_attempted = True
        if self._owner.delete_error is not None:
            raise self._owner.delete_error
        self._owner.deleted = True
        return object()


class _FakeTable:
    def __init__(self, owner: _FakeClient) -> None:
        self._owner = owner

    def insert(self, payload: dict) -> _FakeQuery:
        self._owner.payloads.append(payload)
        return _FakeQuery(self._owner, "insert")

    def delete(self) -> _FakeQuery:
        return _FakeQuery(self._owner, "delete")


class _FakeClient:
    """Minimal stand-in for the supabase client, scripted to fail how we choose."""

    def __init__(
        self, *, insert_error: Exception | None = None, delete_error: Exception | None = None
    ) -> None:
        self.insert_error = insert_error
        self.delete_error = delete_error
        self.payloads: list[dict] = []
        self.inserted = False
        self.deleted = False
        self.delete_attempted = False

    def table(self, _name: str) -> _FakeTable:
        return _FakeTable(self)


def _api_error(code: str) -> Exception:
    from postgrest.exceptions import APIError

    return APIError({"message": f"scripted {code}", "code": code, "hint": None, "details": None})


def test_probe_reports_proof_when_the_insert_succeeds():
    fake = _FakeClient()
    ok, detail = preflight.postgrest_probe("scores", ["a", "b"], client=fake)
    assert ok, detail
    assert fake.inserted and fake.deleted, "the probe row must be cleaned up"


def test_probe_sends_every_requested_column_in_one_payload():
    """#1027 needs BOTH keys proven. Sending them separately, or dropping one,
    would prove a weaker claim than the gate advertises."""
    fake = _FakeClient()
    preflight.postgrest_probe(
        "scores", ["exclusion_keywords", "exclusion_keywords_version"], client=fake
    )
    assert len(fake.payloads) == 1, "one payload, not one per column"
    sent = fake.payloads[0]
    assert "exclusion_keywords" in sent
    assert "exclusion_keywords_version" in sent


@pytest.mark.parametrize("sqlstate", ["23502", "23503", "23505", "23514"])
def test_probe_treats_constraint_violations_as_proof(sqlstate: str):
    """The row was rejected, but only Postgres could have rejected it — which
    means PostgREST already accepted the payload shape."""
    fake = _FakeClient(insert_error=_api_error(sqlstate))
    ok, detail = preflight.postgrest_probe("scores", ["a"], client=fake)
    assert ok, detail
    assert sqlstate in detail


@pytest.mark.parametrize(
    ("code", "why"),
    [
        ("PGRST204", "unknown column — the exact thing being tested for"),
        ("PGRST205", "unknown table"),
        ("PGRST301", "auth failure proves nothing about the schema"),
        ("PGRST302", "auth required"),
    ],
)
def test_probe_fails_closed_on_postgrest_rejections(code: str, why: str):
    fake = _FakeClient(insert_error=_api_error(code))
    ok, detail = preflight.postgrest_probe("scores", ["a"], client=fake)
    assert not ok, f"{code} must not be proof ({why}); got: {detail}"


def test_probe_fails_closed_on_an_unrecognised_api_code():
    """The default must be 'not proof' — including for codes that do not exist
    yet. This is the branch that made the original version fail open."""
    fake = _FakeClient(insert_error=_api_error("PGRST999"))
    ok, detail = preflight.postgrest_probe("scores", ["a"], client=fake)
    assert not ok
    assert "unrecognised" in detail.lower()


def test_probe_fails_closed_on_a_network_error():
    """A URL with nothing listening reported success in the first version."""
    fake = _FakeClient(insert_error=ConnectionRefusedError("[Errno 61] Connection refused"))
    ok, detail = preflight.postgrest_probe("scores", ["a"], client=fake)
    assert not ok
    assert "not proof" in detail


def test_a_cleanup_failure_fails_the_probe_and_names_the_row():
    """Reporting success while leaving a probe row behind would make this tool
    a source of the drift it exists to detect."""
    fake = _FakeClient(delete_error=RuntimeError("delete blew up"))
    ok, detail = preflight.postgrest_probe("scores", ["a"], client=fake)
    assert not ok, "an orphaned probe row must fail the run"
    assert "INSERTED but could not be deleted" in detail
    assert fake.delete_attempted


def test_probe_rejects_a_hostile_column_name_before_any_call():
    fake = _FakeClient()
    with pytest.raises(SystemExit):
        preflight.postgrest_probe("scores", ["a; DROP TABLE scores--"], client=fake)
    assert fake.payloads == [], "nothing may be sent when validation fails"
