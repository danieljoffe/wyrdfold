"""The catalog sync must refuse anything it cannot prove is safe.

This script reads one database and writes another, so the failure that matters
is not "it copied the wrong rows" — it is "it wrote to production". Every test
here is about direction and about sources arriving disabled; the happy path is
one case.
"""

from __future__ import annotations

import pytest

from scripts.sync_catalog_to_staging import (
    PRODUCTION_REF,
    SyncError,
    assert_safe_direction,
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
