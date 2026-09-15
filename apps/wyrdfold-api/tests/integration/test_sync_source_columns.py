"""Every column of `public.sources` must be classified by the staging importer.

WHY THIS EXISTS
`scripts/sync_catalog_to_staging.py` copies production sources into staging and
forces them inert. Twice now the forcing has been INCOMPLETE in a way no unit
test could see, because a unit test only knows the columns its fixture invents:

  * `disabled_at` was copied through. `recover_stale_sources()` re-enables
    exactly `enabled=false AND disabled_at IS NOT NULL AND disabled_at < now()-24h`,
    so an imported source arrived carrying its own re-enable order — and a poll
    that finds none of a source's jobs ARCHIVES them, deleting the catalog the
    importer had just written.
  * `last_error` / `last_error_at` were copied through, so staging reported
    production's failures as its own.

Both were prose failures: the mapping's own comment claimed to be complete
while it wasn't. Prose cannot be tested; this can. The suite reads the LIVE
table and fails on any column the importer classifies as neither "forced inert"
nor "deliberately copied", so adding a column to `sources` forces a decision in
that file rather than a silent default.

Integration because the whole point is the REAL schema — deselected by default,
run via `pytest -m integration`. Self-skips when psql or the DB is unavailable.

SCOPE: this reads the LOCAL database, while the importer reads production. That
is the right trade — migrations are the source of truth for both, CI applies
them here, and a column can only reach production by first existing in
`supabase/migrations/`. It would NOT catch a column created directly against
production by hand, which is out of contract anyway.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from scripts.sync_catalog_to_staging import _COPIED_AS_IS, _INERT_SOURCE_STATE

pytestmark = pytest.mark.integration

PGHOST = os.environ.get("SUPABASE_TEST_DB_HOST", "127.0.0.1")
PGPORT = os.environ.get("SUPABASE_TEST_DB_PORT", "54322")
PGUSER = os.environ.get("SUPABASE_TEST_DB_USER", "postgres")
PGPASSWORD = os.environ.get("SUPABASE_TEST_DB_PASSWORD", "postgres")
PGDATABASE = os.environ.get("SUPABASE_TEST_DB_NAME", "postgres")

_PSQL_BIN = shutil.which("psql")


def _query(sql: str) -> list[str]:
    if _PSQL_BIN is None:
        pytest.skip("psql not available")
    env = {**os.environ, "PGPASSWORD": PGPASSWORD}
    try:
        proc = subprocess.run(  # noqa: S603 — resolved binary, constant SQL
            [_PSQL_BIN, "-h", PGHOST, "-p", PGPORT, "-U", PGUSER, "-d", PGDATABASE,
             "-At", "-c", sql],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        pytest.skip(f"local Postgres unreachable: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"local Postgres unreachable: {proc.stderr.strip()[:200]}")
    return [line for line in proc.stdout.splitlines() if line]


def live_source_columns() -> set[str]:
    cols = set(
        _query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='sources'"
        )
    )
    # Guard the guard: an empty result would make every assertion below pass
    # vacuously, which is exactly the failure mode this suite exists to catch.
    assert cols, "read zero columns for public.sources — the query, not the code, is wrong"
    return cols


def test_every_live_column_is_classified() -> None:
    """The check that would have caught both `disabled_at` and `last_error`."""
    classified = set(_INERT_SOURCE_STATE) | _COPIED_AS_IS
    unclassified = live_source_columns() - classified
    assert not unclassified, (
        f"public.sources has column(s) the staging importer never decided about: "
        f"{sorted(unclassified)}. Add each to _INERT_SOURCE_STATE (forced inert) "
        f"or _COPIED_AS_IS (deliberately copied) in "
        f"scripts/sync_catalog_to_staging.py — do not just append to whichever is "
        f"nearer. Ask: can this column cause staging to POLL, or to report "
        f"production's state as its own? If yes, it must be forced."
    )


def test_no_classified_column_has_been_dropped() -> None:
    """The mirror image, and the reason `prescan_shadow` had to be deleted from
    a sibling allowlist: an entry naming a column that no longer exists never
    matches anything and would let the set rot into meaninglessness."""
    live = live_source_columns()
    stale = (set(_INERT_SOURCE_STATE) | _COPIED_AS_IS) - live
    assert not stale, (
        f"the staging importer classifies column(s) that public.sources no "
        f"longer has: {sorted(stale)}. Remove them so the classification keeps "
        f"describing the real table."
    )


def test_the_two_sets_are_disjoint() -> None:
    """A column cannot be both forced and copied; whichever the code applied
    last would win silently."""
    both = set(_INERT_SOURCE_STATE) & _COPIED_AS_IS
    assert not both, f"classified as BOTH forced and copied: {sorted(both)}"
