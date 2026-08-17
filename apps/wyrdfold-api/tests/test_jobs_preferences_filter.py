"""Read-path preference filtering on /jobs (#60).

Preferences are a per-(user, target) READ-TIME filter over the SHARED, cached
fit score — never a re-grade. These tests pin the three behaviours the feature
must guarantee:

1. The score cutoff filters out low-scoring jobs (folded into ``min_score``,
   server-side, so it's always enforceable — ``scores.score`` always exists).
2. Rows whose backing firewall tag is NULL/absent are KEPT (lenient) — the
   tagger does not backfill, so unknown means keep. (Historical note: until
   the R2 release the tag columns were never SELECTED at all, so the filters
   silently passed everything — the "starved tags" finding, schema audit
   2026-07-30. ``test_jp_select_serves_the_firewall_tags`` pins the wiring.)
3. The employment-type / seniority / location filters DO drop jobs once a
   concrete tag value is present.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.models.targets import TargetPreferences
from app.routers import jobs as jobs_mod
from app.routers.jobs import (
    _apply_preferences_filter,
    _employment_type_passes,
    _job_is_remote,
    _list_jobs_for_target,
    _list_jobs_for_target_two_query,
    _location_pref_passes,
    _preferences_have_post_fetch_filter,
    _seniority_passes,
)
from tests.support.fake_supabase import two_query_supabase


def _job(**cols: Any) -> dict[str, Any]:
    """A posting row. Firewall tag columns are OMITTED by default to model the
    pre-firewall world (column not selected → ``.get()`` is None → unknown)."""
    base: dict[str, Any] = {"id": "job-1", "title": "Engineer", "location": None}
    base.update(cols)
    return base


# ---- has-post-fetch-filter gate -------------------------------------------


def test_no_filter_when_preferences_none() -> None:
    assert _preferences_have_post_fetch_filter(None) is False


def test_score_cutoff_alone_is_not_a_post_fetch_filter() -> None:
    """The cutoff is folded into ``min_score`` server-side, so it must NOT
    force the post-fetch path on its own."""
    prefs = TargetPreferences(pref_score_cutoff=90)
    assert _preferences_have_post_fetch_filter(prefs) is False


def test_employment_type_triggers_post_fetch_filter() -> None:
    prefs = TargetPreferences(pref_employment_types=["full_time"])
    assert _preferences_have_post_fetch_filter(prefs) is True


def test_location_triggers_post_fetch_filter() -> None:
    prefs = TargetPreferences(pref_locations=["berlin"])
    assert _preferences_have_post_fetch_filter(prefs) is True


def test_seniority_triggers_post_fetch_filter() -> None:
    assert (
        _preferences_have_post_fetch_filter(TargetPreferences(pref_seniority_min="staff")) is True
    )


# ---- leniency: NULL / absent tags are KEPT --------------------------------


def test_employment_type_keeps_job_with_absent_tag() -> None:
    """employment_type column not present (pre-firewall) → keep."""
    assert _employment_type_passes(_job(), ["full_time"]) is True


def test_employment_type_keeps_job_with_null_tag() -> None:
    """Column present but NULL (not backfilled) → keep."""
    assert _employment_type_passes(_job(employment_type=None), ["full_time"]) is True


def test_seniority_keeps_job_with_absent_tag() -> None:
    assert _seniority_passes(_job(), seniority_min="senior", seniority_max="director") is True


def test_seniority_keeps_job_with_offladder_tag() -> None:
    """A job tag we can't place on the ladder can't be compared → keep."""
    assert (
        _seniority_passes(
            _job(seniority="fellow"), seniority_min="senior", seniority_max="director"
        )
        is True
    )


def test_location_keeps_job_with_no_metro_and_no_location() -> None:
    assert _location_pref_passes(_job(location=None), locations=["berlin"], remote_ok=False) is True


def test_apply_preferences_filter_is_noop_when_all_tags_absent() -> None:
    """The whole filter is a no-op on pre-firewall rows even with every pref
    set — this is the property that lets the PR ship ahead of the firewall."""
    prefs = TargetPreferences(
        pref_employment_types=["full_time"],
        pref_seniority_min="senior",
        pref_seniority_max="director",
        pref_locations=["berlin"],
        pref_remote_ok=False,
    )
    postings = [_job(id="a"), _job(id="b"), _job(id="c")]
    assert _apply_preferences_filter(postings, prefs) == postings


# ---- employment-type filter with a concrete tag present -------------------


def test_employment_type_drops_mismatched_known_tag() -> None:
    assert _employment_type_passes(_job(employment_type="contract"), ["full_time"]) is False


def test_employment_type_keeps_matching_known_tag_case_insensitive() -> None:
    assert _employment_type_passes(_job(employment_type="Full_Time"), ["full_time"]) is True


def test_apply_preferences_filter_drops_by_employment_type() -> None:
    prefs = TargetPreferences(pref_employment_types=["full_time"])
    postings = [
        _job(id="ft", employment_type="full_time"),
        _job(id="contract", employment_type="contract"),
        _job(id="unknown"),  # no tag → kept (lenient)
    ]
    kept = {p["id"] for p in _apply_preferences_filter(postings, prefs)}
    assert kept == {"ft", "unknown"}


# ---- seniority range with a concrete tag present --------------------------


def test_seniority_drops_below_min() -> None:
    assert (
        _seniority_passes(_job(seniority="ic"), seniority_min="senior", seniority_max=None) is False
    )


def test_seniority_drops_above_max() -> None:
    assert (
        _seniority_passes(_job(seniority="c_level"), seniority_min=None, seniority_max="director")
        is False
    )


def test_seniority_keeps_in_range_inclusive() -> None:
    assert (
        _seniority_passes(_job(seniority="staff"), seniority_min="senior", seniority_max="director")
        is True
    )
    # Inclusive on both ends.
    assert (
        _seniority_passes(
            _job(seniority="senior"), seniority_min="senior", seniority_max="director"
        )
        is True
    )
    assert (
        _seniority_passes(
            _job(seniority="director"), seniority_min="senior", seniority_max="director"
        )
        is True
    )


# ---- location filter with a concrete tag / text present -------------------


def test_location_matches_metro_tag() -> None:
    assert (
        _location_pref_passes(_job(metro="berlin"), locations=["berlin"], remote_ok=False) is True
    )


def test_location_drops_mismatched_metro_when_no_free_text() -> None:
    assert (
        _location_pref_passes(
            _job(metro="nyc", location=None), locations=["berlin"], remote_ok=False
        )
        is False
    )


def test_location_falls_back_to_free_text_location() -> None:
    """No metro tag (pre-firewall) but a free-text location that matches."""
    assert (
        _location_pref_passes(
            _job(location="Berlin, Germany"), locations=["berlin"], remote_ok=False
        )
        is True
    )


def test_location_free_text_avoids_substring_false_positive() -> None:
    """Reuses the location-chip matcher, so 'us' doesn't match 'Austin'."""
    assert (
        _location_pref_passes(_job(location="Austin, TX"), locations=["us"], remote_ok=False)
        is False
    )


def test_remote_ok_lets_remote_role_through_via_flag() -> None:
    assert (
        _location_pref_passes(
            _job(is_remote=True, location="Mars Base", metro="mars"),
            locations=["berlin"],
            remote_ok=True,
        )
        is True
    )


def test_remote_ok_false_blocks_remote_role() -> None:
    assert (
        _location_pref_passes(
            _job(is_remote=True, location="Remote", metro=None),
            locations=["berlin"],
            remote_ok=False,
        )
        is False
    )


def test_remote_detected_via_location_text_when_no_flag() -> None:
    assert _job_is_remote(_job(location="Remote - US")) is True
    assert _job_is_remote(_job(location="Berlin")) is False
    assert _job_is_remote(_job()) is False  # no location → not remote


# ---- score-cutoff fold + routing (endpoint / dispatcher) ------------------


async def test_score_cutoff_folds_into_min_score(monkeypatch: Any) -> None:
    """The per-user cutoff is pushed into the effective ``min_score`` so the
    filter happens server-side over the shared cached score (not a re-grade)."""
    captured: dict[str, Any] = {}

    async def _fake_two_query(supabase: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"postings": [], "next_cursor": None, "total": 0}

    monkeypatch.setattr(jobs_mod, "_list_jobs_for_target_two_query", _fake_two_query)
    # Force the two-query path (employment-type filter) so we can inspect kwargs.
    prefs = TargetPreferences(pref_score_cutoff=75, pref_employment_types=["full_time"])

    await _list_jobs_for_target(
        MagicMock(),
        target_id="target-1",
        page_size=20,
        sort="score",
        ascending=False,
        min_score=75,  # caller already folded the cutoff in
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
        preferences=prefs,
        user_id="user-1",
    )

    assert captured["min_score"] == 75
    assert captured["preferences"] is prefs


async def test_post_fetch_pref_filter_forces_two_query(monkeypatch: Any) -> None:
    """An employment-type/seniority/location preference must bypass the RPC
    (which paginates server-side with no knowledge of the post-fetch filter)
    and use the two-query path."""
    calls = {"rpc": 0, "two_query": 0}

    async def _fake_rpc(supabase: Any, **kwargs: Any) -> dict[str, Any]:
        calls["rpc"] += 1
        return {"postings": [], "next_cursor": None, "total": 0}

    async def _fake_two_query(supabase: Any, **kwargs: Any) -> dict[str, Any]:
        calls["two_query"] += 1
        return {"postings": [], "next_cursor": None, "total": 0}

    monkeypatch.setattr(jobs_mod, "_list_jobs_for_target_rpc", _fake_rpc)
    monkeypatch.setattr(jobs_mod, "_list_jobs_for_target_two_query", _fake_two_query)

    await _list_jobs_for_target(
        MagicMock(),
        target_id="target-1",
        page_size=20,
        sort="score",
        ascending=False,
        min_score=40,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
        preferences=TargetPreferences(pref_locations=["berlin"]),
        user_id="user-1",
    )

    assert calls == {"rpc": 0, "two_query": 1}


async def test_cutoff_only_preferences_keep_rpc_fast_path(monkeypatch: Any) -> None:
    """A cutoff-only preference set (no post-fetch filter) must still allow a fast
    RPC path — the cutoff rides in ``min_score`` and needs no post-fetch work.
    Score sort routes to the cross-target RPC restricted to this one target (#2,
    the index-only denormalized path); it must NOT fall to the scan-everything
    two-query path."""
    calls = {"cross_rpc": 0, "target_rpc": 0, "two_query": 0}

    monkeypatch.setattr(
        jobs_mod,
        "_list_jobs_across_user_targets_rpc",
        AsyncMock(
            side_effect=lambda supabase, **kw: (
                calls.__setitem__("cross_rpc", calls["cross_rpc"] + 1)
                or {"postings": [], "next_cursor": None, "total": None}
            )
        ),
    )
    monkeypatch.setattr(
        jobs_mod,
        "_list_jobs_for_target_rpc",
        AsyncMock(
            side_effect=lambda supabase, **kw: (
                calls.__setitem__("target_rpc", calls["target_rpc"] + 1)
                or {"postings": [], "next_cursor": None, "total": 0}
            )
        ),
    )
    monkeypatch.setattr(
        jobs_mod,
        "_list_jobs_for_target_two_query",
        AsyncMock(
            side_effect=lambda supabase, **kw: (
                calls.__setitem__("two_query", calls["two_query"] + 1)
                or {"postings": [], "next_cursor": None, "total": 0}
            )
        ),
    )

    await _list_jobs_for_target(
        MagicMock(),
        target_id="target-1",
        page_size=20,
        sort="score",
        ascending=False,
        min_score=90,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        cursor={},
        preferences=TargetPreferences(pref_score_cutoff=90),
        user_id="user-1",
    )

    # Score sort → cross-target RPC restricted to the target; no two-query.
    assert calls == {"cross_rpc": 1, "target_rpc": 0, "two_query": 0}


# ---- integration: cutoff filters low scores + leniency end-to-end ----------
#
# These drive the real ``_list_jobs_for_target_two_query`` against
# ``two_query_supabase`` (tests.support.fake_supabase), which HONORS the
# server-side score floor — so a too-low score is dropped by the DB exactly as
# in production — while leaving the post-fetch preference filter to the real
# Python code path.


async def test_integration_cutoff_drops_low_scores() -> None:
    """The score cutoff (folded into ``min_score``) drops jobs below the bar at
    the DB layer — a real filter over the shared cached score, not a re-grade."""
    # GRADED rows (``axis_scores`` present). The floor only judges rows that
    # carry a real fit score — ``scoring_status: 'complete'`` alone does NOT
    # make a row graded (see ``_is_pending``), and ``_apply_score_floor``
    # exempts ``axis_scores IS NULL`` rows by design (#47). Without the axes
    # these three would be Pending, i.e. exempt, and the cutoff this test is
    # about would correctly not apply to them.
    scores = [
        {
            "job_posting_id": "hi",
            "score": 90,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 90},
        },
        {
            "job_posting_id": "mid",
            "score": 55,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 55},
        },
        {
            "job_posting_id": "lo",
            "score": 20,
            "score_breakdown": {},
            "scoring_status": "complete",
            "axis_scores": {"title_fit": 20},
        },
    ]
    jobs = {
        "hi": {"id": "hi", "title": "hi", "location": None},
        "mid": {"id": "mid", "title": "mid", "location": None},
        "lo": {"id": "lo", "title": "lo", "location": None},
    }
    sb = two_query_supabase(scores, jobs)

    # Caller folds pref_score_cutoff=60 into min_score before calling.
    result = await _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        cursor={},
        page_size=10,
        sort="score",
        ascending=False,
        min_score=60,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        preferences=TargetPreferences(pref_score_cutoff=60, pref_employment_types=["full_time"]),
    )

    kept = {p["id"] for p in result["postings"]}
    assert kept == {"hi"}  # mid (55) and lo (20) are below the 60 cutoff


async def test_integration_null_tags_kept_with_active_pref_filter() -> None:
    """With employment_type + seniority + location preferences ALL set but the
    job tags absent (pre-firewall), every above-cutoff row is KEPT — proving
    the post-fetch filter is lenient end-to-end through the real two-query
    pipeline."""
    scores = [
        {"job_posting_id": "a", "score": 80, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "b", "score": 70, "score_breakdown": {}, "scoring_status": "complete"},
    ]
    jobs = {
        # No employment_type / seniority / metro / is_remote columns at all.
        "a": {"id": "a", "title": "a", "location": None},
        "b": {"id": "b", "title": "b", "location": None},
    }
    sb = two_query_supabase(scores, jobs)

    result = await _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        cursor={},
        page_size=10,
        sort="score",
        ascending=False,
        min_score=40,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        preferences=TargetPreferences(
            pref_score_cutoff=40,
            pref_employment_types=["full_time"],
            pref_seniority_min="senior",
            pref_seniority_max="director",
            pref_locations=["berlin"],
            pref_remote_ok=False,
        ),
    )

    assert {p["id"] for p in result["postings"]} == {"a", "b"}
    assert result["total"] == 2


async def test_integration_employment_type_filter_drops_known_mismatch() -> None:
    """Once a concrete employment_type tag is present, the post-fetch filter
    drops mismatches while keeping matches AND unknown-tag rows — through the
    real two-query pipeline (total reflects the post-filter count)."""
    scores = [
        {"job_posting_id": "ft", "score": 80, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "ct", "score": 75, "score_breakdown": {}, "scoring_status": "complete"},
        {"job_posting_id": "unk", "score": 70, "score_breakdown": {}, "scoring_status": "complete"},
    ]
    jobs = {
        "ft": {"id": "ft", "title": "ft", "location": None, "employment_type": "full_time"},
        "ct": {"id": "ct", "title": "ct", "location": None, "employment_type": "contract"},
        "unk": {"id": "unk", "title": "unk", "location": None},  # no tag → kept
    }
    sb = two_query_supabase(scores, jobs)

    result = await _list_jobs_for_target_two_query(
        sb,
        target_id="t-1",
        cursor={},
        page_size=10,
        sort="score",
        ascending=False,
        min_score=40,
        status=None,
        company=None,
        search=None,
        exclude_terms=[],
        only_terms=[],
        preferences=TargetPreferences(pref_score_cutoff=40, pref_employment_types=["full_time"]),
    )

    assert {p["id"] for p in result["postings"]} == {"ft", "unk"}
    assert result["total"] == 2  # post-filter count, contract dropped


def test_jp_select_serves_the_firewall_tags() -> None:
    """The starved-tags regression pin (schema audit Group B addendum): the
    preference filters only work if the tag columns actually ARRIVE. Keep all
    four in the list/detail projections — removing one silently reverts its
    filter to pass-everything (the failure mode that went unnoticed for a
    year, because lenient-on-absence looks identical to no-filter)."""
    for col in ("employment_type", "seniority", "metro", "is_remote"):
        assert col in jobs_mod._JP_SELECT_COLS
        assert col in jobs_mod._JP_DETAIL_SELECT_COLS
