"""The catalog sync must refuse anything it cannot prove is safe.

This script reads one database and writes another, so the failure that matters
is not "it copied the wrong rows" — it is "it wrote to production". Every test
here is about direction and about sources arriving disabled; the happy path is
one case.
"""

from __future__ import annotations

import pytest

from scripts.sync_catalog_to_staging import (
    MAX_LIMIT,
    PRODUCTION_REF,
    SyncError,
    assert_safe_direction,
    checked_limit,
    disabled_source,
    project_identity,
)

STAGING_REF = "dyczsvaoqhvnafwwxuvf"
PROD_POOLER = f"postgresql://postgres.{PRODUCTION_REF}:pw@aws-1.pooler.supabase.com:5432/postgres"
STAGING_REST = f"https://{STAGING_REF}.supabase.co"
PROD_REST = f"https://{PRODUCTION_REF}.supabase.co"


# ---- identity ---------------------------------------------------------------


def test_identity_from_rest_url() -> None:
    assert project_identity(STAGING_REST) == STAGING_REF


def test_identity_from_pooler_url_reads_the_username() -> None:
    """The pooler carries the ref in the USERNAME, not the host — miss that and
    production's connection string looks like an unknown target."""
    assert project_identity(PROD_POOLER) == PRODUCTION_REF


def test_identity_from_direct_db_host() -> None:
    assert project_identity(f"postgresql://postgres:pw@db.{STAGING_REF}.supabase.co:5432/postgres") == STAGING_REF


def test_localhost_is_local() -> None:
    assert project_identity("http://127.0.0.1:54321") == "local"


def test_unrecognised_shape_is_never_a_match() -> None:
    """Must not collapse to something that could equal a real ref."""
    a = project_identity("https://example.com")
    assert a.startswith("unknown:")
    assert a != project_identity("https://other.example.org")


# ---- direction: the refusal battery ----------------------------------------


def test_prod_to_staging_is_allowed() -> None:
    """The ONLY permissive path."""
    src, dst = assert_safe_direction(PROD_POOLER, STAGING_REST, STAGING_REF)
    assert (src, dst) == (PRODUCTION_REF, STAGING_REF)


def test_writing_to_production_refuses() -> None:
    """The one that matters. Reversed arguments must not copy staging's
    fictional catalog into the live database."""
    with pytest.raises(SyncError, match="resolves to PRODUCTION"):
        assert_safe_direction(STAGING_REST, PROD_REST, PRODUCTION_REF)


def test_writing_to_production_via_pooler_url_also_refuses() -> None:
    """Same refusal when production is named in the other URL shape — the
    check is on identity, not on string form."""
    with pytest.raises(SyncError, match="resolves to PRODUCTION"):
        assert_safe_direction(STAGING_REST, PROD_POOLER, PRODUCTION_REF)


def test_unidentifiable_write_target_refuses() -> None:
    with pytest.raises(SyncError, match="could not identify the write target"):
        assert_safe_direction(PROD_POOLER, "https://something.else.example", "whatever")


def test_same_project_both_ends_refuses() -> None:
    """Copying a database onto itself is a no-op at best and a sign that one of
    the two variables is wrong at worst."""
    with pytest.raises(SyncError, match="SAME project"):
        assert_safe_direction(STAGING_REST, STAGING_REST, STAGING_REF)


def test_confirmation_must_name_the_real_destination() -> None:
    """The caller states the destination instead of inheriting whatever the
    environment held. A stale STAGING_SUPABASE_URL pointing somewhere else is
    caught here rather than discovered afterwards."""
    other = "aaaaaaaaaaaaaaaaaaaa"
    with pytest.raises(SyncError, match="--confirm-write-to"):
        assert_safe_direction(PROD_POOLER, STAGING_REST, other)


def test_confirmation_cannot_be_used_to_authorise_production() -> None:
    """Naming production in the confirmation flag must not unlock it — the
    production refusal is checked first and has no override."""
    with pytest.raises(SyncError, match="resolves to PRODUCTION"):
        assert_safe_direction(STAGING_REST, PROD_REST, PRODUCTION_REF)


# ---- --limit is a bounded, deliberate row count ----------------------------


def test_limit_rejects_zero() -> None:
    """Postgres accepts LIMIT 0 and copies nothing, which reads as success."""
    with pytest.raises(SyncError, match="at least 1"):
        checked_limit(0)


def test_limit_rejects_negative() -> None:
    """Postgres rejects a negative LIMIT itself ("LIMIT must not be negative"),
    but that surfaces as a SQL error deep inside a psql call and says nothing
    about the flag the operator mistyped. Refuse at the boundary, by name."""
    with pytest.raises(SyncError, match="at least 1"):
        checked_limit(-1)


def test_limit_rejects_over_cap() -> None:
    """An extra zero should be a refusal, not a long transfer nobody meant."""
    with pytest.raises(SyncError, match="exceeds the"):
        checked_limit(MAX_LIMIT + 1)


def test_limit_accepts_the_cap_exactly() -> None:
    assert checked_limit(MAX_LIMIT) == MAX_LIMIT


# ---- the WRITE boundary: what actually leaves the process ------------------
#
# The refusal battery above proves the DECISION. These prove the PAYLOAD. The
# production incident this script must not cause is an enabled source being
# copied and then polled — and a test that only exercises helpers cannot catch
# that transform being removed, reordered, or skipped for one branch.


def test_disabled_source_forces_enabled_false() -> None:
    out = disabled_source({"id": "x", "board_token": "t", "enabled": True})
    assert out["enabled"] is False


def test_disabled_source_resets_polling_bookkeeping() -> None:
    """Staging must not inherit production's failure counters and immediately
    look unhealthy, nor a last_polled_at that makes it seem recently active."""
    out = disabled_source(
        {"id": "x", "enabled": True, "last_polled_at": "2026-09-01T00:00:00Z", "consecutive_failures": 7}
    )
    assert out["last_polled_at"] is None
    assert out["consecutive_failures"] == 0


def test_disabled_source_does_not_mutate_the_input() -> None:
    """The caller's row is production data read moments earlier; mutating it in
    place would make the transform order-dependent and hard to reason about."""
    row = {"id": "x", "enabled": True}
    disabled_source(row)
    assert row["enabled"] is True


def test_disabled_source_preserves_everything_else() -> None:
    row = {"id": "x", "board_token": "acme", "company_name": "Acme", "provider": "greenhouse", "enabled": True}
    out = disabled_source(row)
    assert out["board_token"] == "acme" and out["company_name"] == "Acme" and out["provider"] == "greenhouse"


def test_every_source_written_by_main_is_disabled(monkeypatch) -> None:
    """END TO END through main(): whatever the read returns, nothing enabled
    may reach the write. This is the assertion that fails if the transform is
    dropped — the helper tests above cannot notice it never being called."""
    from scripts import sync_catalog_to_staging as mod

    monkeypatch.setenv("PROD_DB_URL", PROD_POOLER)
    monkeypatch.setenv("STAGING_SUPABASE_URL", STAGING_REST)
    monkeypatch.setenv("STAGING_SERVICE_KEY", "sb_secret_test")

    jobs = [{"id": f"j{i}", "source_id": "11111111-2222-4333-8444-555555555555"} for i in range(3)]
    reads = [jobs, [{"id": "11111111-2222-4333-8444-555555555555", "enabled": True, "consecutive_failures": 9}]]
    monkeypatch.setattr(mod, "psql_json", lambda *a, **k: reads.pop(0))

    written: dict[str, list] = {}
    monkeypatch.setattr(mod, "post_rows", lambda _u, _k, table, rows: written.setdefault(table, rows) and 0)

    assert mod.main(["--confirm-write-to", STAGING_REF, "--limit", "3"]) == 0
    assert written["sources"], "sources were never written"
    assert all(s["enabled"] is False for s in written["sources"]), "an ENABLED source reached the write"
    assert all(s["consecutive_failures"] == 0 for s in written["sources"])


def test_main_refuses_before_writing_anything(monkeypatch) -> None:
    """A refused direction must not reach the write boundary at all."""
    from scripts import sync_catalog_to_staging as mod

    monkeypatch.setenv("PROD_DB_URL", STAGING_REST)
    monkeypatch.setenv("STAGING_SUPABASE_URL", PROD_REST)
    monkeypatch.setenv("STAGING_SERVICE_KEY", "k")
    calls: list = []
    monkeypatch.setattr(mod, "psql_json", lambda *a, **k: calls.append("read") or [])
    monkeypatch.setattr(mod, "post_rows", lambda *a, **k: calls.append("write") or 0)

    assert mod.main(["--confirm-write-to", PRODUCTION_REF]) == 2
    assert calls == [], "a refused run touched the database"
