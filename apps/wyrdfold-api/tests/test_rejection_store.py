"""Unit contract of the persistent Phase-1 rejection store
(``app/services/relevance/rejection_store.py``).

The poller-path behavior (synthetic verdicts, attempted-set semantics,
profile-version re-judging through a real cycle) lives in ``test_poller.py``;
this file pins the store's own contract: key normalization, TTL filtering,
chunking, the TTL=0 kill switch, and — most load-bearing — the failure
posture (fail-open reads, swallowed writes).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.config import settings as live_settings
from app.services.relevance.rejection_store import (
    _IN_CHUNK_SIZE,
    fetch_rejected_titles,
    normalize_title,
    record_rejections,
)
from tests.support.fake_phase1_store import phase1_store_supabase


def _target(profile_version: int = 1) -> MagicMock:
    target = MagicMock()
    target.id = "tgt-1"
    target.profile_version = profile_version
    return target


@pytest.fixture(autouse=True)
def _enable_store(monkeypatch):
    monkeypatch.setattr(live_settings, "phase1_rejection_ttl_hours", 24.0)


@pytest.mark.asyncio
async def test_roundtrip_normalizes_title_keys(monkeypatch):
    """Record with messy whitespace/case; fetch with a differently-messy
    variant — both normalize to the same key. Unrelated titles miss."""
    supabase = phase1_store_supabase()
    target = _target()

    await record_rejections(supabase, target, [("Brand  New\tRole", 88)])
    row = supabase._phase1_rejections.rows[("tgt-1", 1, "brand new role")]
    assert row["confidence"] == 88
    assert row["model"] == live_settings.phase1_triage_model

    hits = await fetch_rejected_titles(supabase, target, ["BRAND NEW ROLE", "Other Role"])
    assert hits == {"brand new role"}
    # The caller-side membership test is on the normalized form.
    assert normalize_title("BRAND NEW ROLE") in hits
    assert normalize_title("Other Role") not in hits


@pytest.mark.asyncio
async def test_profile_version_is_part_of_the_key():
    """A rejection persisted under profile v1 must not answer for v2 — the
    poisoned-row sabotage: the old row provably exists, and provably does
    not suppress.

    The v1 fetch is the positive control: the store FAIL-OPENS to the empty
    set on read errors, so a bare ``hits == set()`` would also pass with a
    broken read path. The same call shape returning a hit for v1 proves the
    v2 miss is a genuine key mismatch, not a swallowed error."""
    supabase = phase1_store_supabase()

    await record_rejections(supabase, _target(profile_version=1), [("Brand New Role", None)])
    assert ("tgt-1", 1, "brand new role") in supabase._phase1_rejections.rows  # precondition

    # Positive control — read path provably works for the matching key.
    assert await fetch_rejected_titles(
        supabase, _target(profile_version=1), ["Brand New Role"]
    ) == {"brand new role"}
    # The actual claim — same row, bumped profile version, no answer.
    hits = await fetch_rejected_titles(supabase, _target(profile_version=2), ["Brand New Role"])
    assert hits == set()


@pytest.mark.asyncio
async def test_expired_rows_stop_matching():
    """A row older than the TTL is invisible to the read path (retention
    deletes it later; correctness never waits for the sweep)."""
    supabase = phase1_store_supabase()
    target = _target()

    await record_rejections(supabase, target, [("Brand New Role", None)])
    key = ("tgt-1", 1, "brand new role")
    assert key in supabase._phase1_rejections.rows  # precondition: row exists
    # Poison judged_at to just past the 24h TTL.
    supabase._phase1_rejections.rows[key]["judged_at"] = (
        datetime.now(UTC) - timedelta(hours=25)
    ).isoformat()

    assert await fetch_rejected_titles(supabase, target, ["Brand New Role"]) == set()


@pytest.mark.asyncio
async def test_ttl_zero_disables_both_directions(monkeypatch):
    """TTL=0 is the kill switch: no reads, no writes, no table access at
    all — the poller behaves as if the store didn't exist."""
    monkeypatch.setattr(live_settings, "phase1_rejection_ttl_hours", 0.0)
    supabase = phase1_store_supabase()
    target = _target()

    await record_rejections(supabase, target, [("Brand New Role", None)])
    assert await fetch_rejected_titles(supabase, target, ["Brand New Role"]) == set()
    supabase.table.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_chunks_large_candidate_batches():
    """A candidate set past the ``.in_()`` URL-safety chunk splits into
    multiple reads, and the union still finds every persisted rejection."""
    supabase = phase1_store_supabase()
    target = _target()
    titles = [f"Role Number {i}" for i in range(_IN_CHUNK_SIZE * 2 + 7)]

    await record_rejections(supabase, target, [(t, None) for t in titles])
    hits = await fetch_rejected_titles(supabase, target, titles)

    assert hits == {normalize_title(t) for t in titles}
    assert supabase._phase1_rejections.select_calls == 3  # 50 + 50 + 17


@pytest.mark.asyncio
async def test_record_dedupes_within_one_batch():
    """Two raw titles that normalize identically collapse to one upsert row
    — PostgREST rejects duplicate keys within a single upsert payload, so
    this is load-bearing, not cosmetic."""
    supabase = phase1_store_supabase()

    await record_rejections(
        supabase, _target(), [("Brand  New Role", 70), ("brand new role", 90)]
    )
    assert list(supabase._phase1_rejections.rows) == [("tgt-1", 1, "brand new role")]


@pytest.mark.asyncio
async def test_record_refreshes_judged_at_on_conflict():
    """A re-judged rejection must supply ``judged_at`` explicitly: the
    column default only applies on INSERT, and a conflict-update keeping
    the original stamp would expire the rejection on the wrong clock."""
    supabase = phase1_store_supabase()
    target = _target()
    key = ("tgt-1", 1, "brand new role")

    await record_rejections(supabase, target, [("Brand New Role", None)])
    stale = (datetime.now(UTC) - timedelta(hours=23)).isoformat()
    supabase._phase1_rejections.rows[key]["judged_at"] = stale

    await record_rejections(supabase, target, [("Brand New Role", None)])
    assert supabase._phase1_rejections.rows[key]["judged_at"] > stale


@pytest.mark.asyncio
async def test_read_failure_fails_open_to_all_misses(caplog):
    """A broken store means paying the LLM again — never a broken cycle.
    Every candidate reads as a miss and the failure is WARNING-visible."""
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("connection refused")

    with caplog.at_level("WARNING"):
        hits = await fetch_rejected_titles(supabase, _target(), ["Brand New Role"])

    assert hits == set()
    assert any("read failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_write_failure_is_swallowed(caplog):
    """A lost write re-pays one verdict next cycle; it must never raise
    into the poll loop."""
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("connection refused")

    with caplog.at_level("WARNING"):
        await record_rejections(supabase, _target(), [("Brand New Role", None)])

    assert any("write failed" in r.message for r in caplog.records)
