"""#698: the scores index set is a closed, deliberate list.

scores is the app's hottest write table; every index on it taxes every
poller insert and every recency update. The 2026-09-04 measurement pass
(issue #698) EXPLAINed each reachable query shape and dropped the six
indexes no live plan needs — including one (a partial on
``scoring_status <> 'complete'`` covering most of the table) that made its
only consumer 8x SLOWER via a bad BitmapAnd.

This pins the EXACT set, both directions:

* a dropped index reappearing (someone restores it from an old migration
  or a well-meaning "add an index" fix) fails here and points at the
  evidence;
* the drop migration not shipping leaves the write tax in place — the
  absent-set check fails on any schema that still carries them;
* any NEW index must be added to the keep-set consciously, with the same
  which-live-shape-needs-it justification the #698 pass applied.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration

PGHOST = os.environ.get("SUPABASE_TEST_DB_HOST", "127.0.0.1")
PGPORT = os.environ.get("SUPABASE_TEST_DB_PORT", "54322")
PGUSER = os.environ.get("SUPABASE_TEST_DB_USER", "postgres")
PGPASSWORD = os.environ.get("SUPABASE_TEST_DB_PASSWORD", "postgres")
PGDATABASE = os.environ.get("SUPABASE_TEST_DB_NAME", "postgres")

_PSQL_BIN = shutil.which("psql")

# The deliberate set (#698). Each entry names the live shape that needs it.
EXPECTED_SCORES_INDEXES = frozenset(
    {
        # Constraints.
        "job_target_scores_pkey",  # pkey; also the sweep fn's keyset cursor
        "job_target_scores_job_posting_id_target_id_key",  # upserts + every jpid lookup
        # The cross-target list RPC's index-only scan (#365 / 2026-08-09 INCLUDE).
        "idx_scores_live_dedup",
        # The one general (target_id, excluded, ...) entry index: phase-2
        # queue, funnel, insights entries, export, FK-side target scans.
        "idx_jts_target_score",
        # #996 candidate windows (ordered, partial, NULLS LAST).
        "idx_scores_pending_window",
        "idx_scores_graded_window",
    }
)

# Dropped by 20260906000000 with measured evidence — must NOT come back
# without re-running the #698 measurement.
DROPPED_SCORES_INDEXES = frozenset(
    {
        "idx_scores_target_excl_score_jpid",
        "idx_scores_target_excl_recency_jpid",
        "idx_jts_job",
        "idx_jts_target",
        "scores_target_recency_idx",
        "idx_jts_scoring_status",
    }
)


def _psql(query: str) -> str | None:
    """Constant SQL via the resolved psql path (same acknowledgement as
    ``test_privilege_invariants``); None when the stack isn't up → skip."""
    if _PSQL_BIN is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — constant SQL, resolved psql path
            [
                _PSQL_BIN,
                "-h",
                PGHOST,
                "-p",
                PGPORT,
                "-U",
                PGUSER,
                "-d",
                PGDATABASE,
                "-t",
                "-A",
                "-q",
                "-c",
                query,
            ],
            env={**os.environ, "PGPASSWORD": PGPASSWORD},
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def _live_indexes() -> frozenset[str]:
    out = _psql(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'scores' ORDER BY 1"
    )
    if out is None:
        pytest.skip(
            "local Supabase DB not reachable via psql at "
            f"{PGHOST}:{PGPORT} — start the local stack to run this suite"
        )
    return frozenset(out.splitlines())


def test_scores_index_set_is_exactly_the_deliberate_list(
    _live_indexes: frozenset[str],
) -> None:
    unexpected = _live_indexes - EXPECTED_SCORES_INDEXES
    missing = EXPECTED_SCORES_INDEXES - _live_indexes
    assert not unexpected, (
        f"unexpected index(es) on scores: {sorted(unexpected)} — every scores "
        "index taxes the poller's hottest write path. If deliberate, add it "
        "to EXPECTED_SCORES_INDEXES with the live shape that needs it (#698)."
    )
    assert not missing, (
        f"missing index(es) on scores: {sorted(missing)} — a live query shape "
        "lost its index (or the migration set drifted)."
    )


def test_dropped_scores_indexes_stay_dropped(_live_indexes: frozenset[str]) -> None:
    resurrected = _live_indexes & DROPPED_SCORES_INDEXES
    assert not resurrected, (
        f"{sorted(resurrected)} were dropped by 20260906000000 with measured "
        "evidence (#698: zero live plans, or actively harmful). Re-adding one "
        "requires re-running that measurement, not restoring an old definition."
    )
