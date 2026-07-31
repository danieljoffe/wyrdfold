"""Tests for Phase 2 orchestration (#6).

Covers the gate / re-grade predicate, progressive batching, and
``run_phase2_for_jobs``: promising-only selection, the daily cap trim,
newest-first ordering, and the graded count. The actual grader
(``score_with_phase2_and_persist``) and the quota counter are stubbed —
this exercises the policy, not the LLM or DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.models.experience import OptimizedPayload
from app.models.targets import (
    CategoryProfile,
    JobTarget,
    ScoringProfile,
    SeniorityProfile,
)
from app.services.fit.job_fit import AxisScores, JobFitResult
from app.services.fit.phase2_runner import (
    PHASE2_BATCH_SIZE,
    PHASE2_FIRST_BATCH,
    _needs_phase2,
    _progressive_batches,
    run_phase2_for_jobs,
)

_RUNNER = "app.services.fit.phase2_runner"


def _target(profile_version: int = 1) -> JobTarget:
    return JobTarget(
        id="t-1",
        label="Staff Frontend Engineer",
        scoring_profile=ScoringProfile(
            categories={"core": CategoryProfile(keywords={"x": 1}, weight=2.0)},
            seniority=SeniorityProfile(signals=["staff"]),
        ),
        profile_version=profile_version,
        app_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _payload() -> OptimizedPayload:
    return OptimizedPayload(summary="...", roles=[], skills=[], outcomes=[])


def _fit() -> JobFitResult:
    return JobFitResult(
        fit_score=82,
        axes=AxisScores(title_fit=90, skills_fit=80, seniority_fit=85, domain_fit=70),
        reasoning="ok",
    )


# ---- _needs_phase2 ---------------------------------------------------------


def test_needs_phase2_requires_promising_true() -> None:
    # Not promising / unknown → never a candidate.
    assert _needs_phase2(None, "stage2", 1, 1) is False
    assert _needs_phase2(False, "stage2", 1, 1) is False
    # Promising + not yet finished → candidate.
    assert _needs_phase2(True, "stage2", 1, 1) is True


def test_needs_phase2_skips_already_current() -> None:
    # Complete at the current version → skip (re-grade contract).
    assert _needs_phase2(True, "complete", 2, 2) is False
    assert _needs_phase2(True, "complete", 3, 2) is False


def test_needs_phase2_regrades_stale_complete() -> None:
    # Complete but at an older profile version → re-grade.
    assert _needs_phase2(True, "complete", 1, 2) is True


# ---- _progressive_batches --------------------------------------------------


def test_progressive_batches_small_set_single_batch() -> None:
    assert _progressive_batches(["a", "b"], PHASE2_FIRST_BATCH, PHASE2_BATCH_SIZE) == [["a", "b"]]


def test_progressive_batches_first_small_then_large() -> None:
    items = [str(i) for i in range(75)]
    batches = _progressive_batches(items, PHASE2_FIRST_BATCH, PHASE2_BATCH_SIZE)
    # 75 = 20 (first) + 50 + 5.
    assert [len(b) for b in batches] == [20, 50, 5]
    # Order preserved, no drops.
    assert [x for b in batches for x in b] == items


def test_progressive_batches_empty() -> None:
    assert _progressive_batches([], PHASE2_FIRST_BATCH, PHASE2_BATCH_SIZE) == []


# ---- run_phase2_for_jobs ---------------------------------------------------


class _StateChain:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def select(self, *_a: Any, **_kw: Any) -> _StateChain:
        return self

    def eq(self, *_a: Any, **_kw: Any) -> _StateChain:
        return self

    def in_(self, *_a: Any, **_kw: Any) -> _StateChain:
        return self

    def execute(self) -> Any:
        return MagicMock(data=self._rows)


def _supabase(state_rows: list[dict[str, Any]]) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = lambda name: _StateChain(state_rows)
    return sb


def _patch_grader(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    graded: list[str] = []

    async def fake_score(_sb: Any, _llm: Any, *, job_posting_id: str, **_kw: Any) -> JobFitResult:
        graded.append(job_posting_id)
        return _fit()

    monkeypatch.setattr(f"{_RUNNER}.score_with_phase2_and_persist", fake_score)
    return graded


def _patch_quota(monkeypatch: pytest.MonkeyPatch, quota: int) -> None:
    monkeypatch.setattr(f"{_RUNNER}.phase2_quota_remaining", lambda *_a, **_kw: quota)


@pytest.mark.asyncio
async def test_grades_only_promising_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 100)
    rows = [
        {
            "job_posting_id": "j-yes",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
        },
        {
            "job_posting_id": "j-no-prom",
            "promising": False,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
        },
        {
            "job_posting_id": "j-done",
            "promising": True,
            "scoring_status": "complete",
            "scored_profile_version": 1,
        },
        # No scores row at all for j-missing (Phase 1 dropped it).
    ]
    jobs = [
        {"id": "j-yes", "title": "FE", "description_html": "<p>x</p>"},
        {"id": "j-no-prom", "title": "PM", "description_html": ""},
        {"id": "j-done", "title": "FE2", "description_html": ""},
        {"id": "j-missing", "title": "??", "description_html": ""},
    ]

    n = await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )

    assert n == 1
    assert graded == ["j-yes"]


@pytest.mark.asyncio
async def test_daily_cap_trims_to_quota_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 2)  # only two grades left today
    rows = [
        {
            "job_posting_id": f"j{i}",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
        }
        for i in range(4)
    ]
    # j3 newest, j0 oldest — cap should pick the two freshest.
    jobs = [
        {
            "id": "j0",
            "title": "a",
            "description_html": "",
            "first_seen_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": "j1",
            "title": "b",
            "description_html": "",
            "first_seen_at": "2026-02-01T00:00:00+00:00",
        },
        {
            "id": "j2",
            "title": "c",
            "description_html": "",
            "first_seen_at": "2026-03-01T00:00:00+00:00",
        },
        {
            "id": "j3",
            "title": "d",
            "description_html": "",
            "first_seen_at": "2026-04-01T00:00:00+00:00",
        },
    ]

    n = await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )

    assert n == 2
    assert set(graded) == {"j3", "j2"}


@pytest.mark.asyncio
async def test_zero_quota_grades_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 0)
    rows = [
        {
            "job_posting_id": "j0",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
        }
    ]
    jobs = [{"id": "j0", "title": "a", "description_html": ""}]

    n = await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )

    assert n == 0
    assert graded == []


@pytest.mark.asyncio
async def test_empty_jobs_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graded = _patch_grader(monkeypatch)
    # Quota counter must not even be consulted on empty input.
    monkeypatch.setattr(
        f"{_RUNNER}.phase2_quota_remaining",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("called")),
    )
    n = await run_phase2_for_jobs(
        MagicMock(), MagicMock(), target=_target(1), payload=_payload(), jobs=[]
    )
    assert n == 0
    assert graded == []


# ---- Phase 1 confidence ordering ------------------------------------------


@pytest.mark.asyncio
async def test_orders_candidates_by_confidence_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the daily cap bites, the highest-confidence jobs grade first."""
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 2)  # only two grades allowed
    # 4 promising candidates at the same first_seen_at; confidences differ.
    rows = [
        {
            "job_posting_id": "j-low-1",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 30,
        },
        {
            "job_posting_id": "j-high",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 95,
        },
        {
            "job_posting_id": "j-mid",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 70,
        },
        {
            "job_posting_id": "j-low-2",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 40,
        },
    ]
    jobs = [
        {
            "id": jid,
            "title": "x",
            "description_html": "",
            "first_seen_at": "2026-04-01T00:00:00+00:00",
        }
        for jid in ("j-low-1", "j-high", "j-mid", "j-low-2")
    ]
    n = await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )
    assert n == 2
    # j-high (95) + j-mid (70) — NOT the lower-confidence ones.
    assert set(graded) == {"j-high", "j-mid"}


@pytest.mark.asyncio
async def test_null_confidence_sorts_below_any_real_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy rows (no confidence captured yet) still grade — but real-
    confidence rows go first when there's contention for the cap."""
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 1)
    rows = [
        {
            "job_posting_id": "j-legacy",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": None,
        },
        {
            "job_posting_id": "j-confident",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 50,
        },
    ]
    jobs = [
        {
            "id": "j-legacy",
            "title": "a",
            "description_html": "",
            "first_seen_at": "2026-04-02T00:00:00+00:00",
        },  # NEWER
        {
            "id": "j-confident",
            "title": "b",
            "description_html": "",
            "first_seen_at": "2026-04-01T00:00:00+00:00",
        },  # OLDER but has confidence
    ]
    n = await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )
    assert n == 1
    # Confidence wins over first_seen_at — j-confident graded, j-legacy deferred.
    assert graded == ["j-confident"]


@pytest.mark.asyncio
async def test_confidence_ties_break_by_first_seen_at_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equal confidence → freshest first."""
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 1)
    rows = [
        {
            "job_posting_id": "j-old",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 80,
        },
        {
            "job_posting_id": "j-new",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 80,
        },
    ]
    jobs = [
        {
            "id": "j-old",
            "title": "a",
            "description_html": "",
            "first_seen_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "id": "j-new",
            "title": "b",
            "description_html": "",
            "first_seen_at": "2026-04-01T00:00:00+00:00",
        },
    ]
    n = await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )
    assert n == 1
    assert graded == ["j-new"]


# ---- cosine grading priority (#9) -----------------------------------------
# NB the confidence tests above mock no embeddings, so cosine_scores_batch
# returns {} and cosine defaults uniformly to -1 — confidence/recency become
# the effective sort (backward-compatible fallback). These pin the PRIMARY key:
# cosine, the fit-predictive signal, which overrides confidence when present.


@pytest.mark.asyncio
async def test_orders_candidates_by_cosine_desc_over_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cosine is the primary key: when the cap bites, the highest-cosine jobs
    grade first — beating a higher phase1_confidence (which doesn't predict
    fit). cosine is INVERTED vs confidence here to prove which wins."""
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 2)
    rows = [
        {
            "job_posting_id": "j-hicos",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 30,
        },
        {
            "job_posting_id": "j-midcos",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 90,
        },
        {
            "job_posting_id": "j-locos",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 95,
        },
    ]
    jobs = [
        {
            "id": jid,
            "title": "x",
            "description_html": "",
            "first_seen_at": "2026-04-01T00:00:00+00:00",
        }
        for jid in ("j-hicos", "j-midcos", "j-locos")
    ]

    async def fake_cosines(_sb: Any, _t: Any, ids: list[str], **_k: Any) -> dict[str, float]:
        return {"j-hicos": 0.60, "j-midcos": 0.45, "j-locos": 0.20}

    monkeypatch.setattr(f"{_RUNNER}.cosine_scores_batch", fake_cosines)

    n = await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )
    assert n == 2
    # Top-2 by cosine — NOT j-locos, despite its confidence 95.
    assert set(graded) == {"j-hicos", "j-midcos"}


@pytest.mark.asyncio
async def test_missing_cosine_sorts_below_a_scored_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job with a cosine score outranks one without (un-embedded / absent from
    the batch → -1, sorts last), even at higher confidence."""
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 1)
    rows = [
        {
            "job_posting_id": "j-scored",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 50,
        },
        {
            "job_posting_id": "j-unscored",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 99,
        },
    ]
    jobs = [
        {
            "id": jid,
            "title": "x",
            "description_html": "",
            "first_seen_at": "2026-04-01T00:00:00+00:00",
        }
        for jid in ("j-scored", "j-unscored")
    ]

    async def fake_cosines(_sb: Any, _t: Any, ids: list[str], **_k: Any) -> dict[str, float]:
        return {"j-scored": 0.40}  # j-unscored omitted (no vector)

    monkeypatch.setattr(f"{_RUNNER}.cosine_scores_batch", fake_cosines)

    n = await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )
    assert n == 1
    assert graded == ["j-scored"]


# ---- seniority pre-gate (#902) --------------------------------------------


def _director_target() -> JobTarget:
    t = _target(1)
    t.seniority_hint = "director"
    return t


def _gate_rows(ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "job_posting_id": jid,
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
            "phase1_confidence": 90,
        }
        for jid in ids
    ]


@pytest.mark.asyncio
async def test_seniority_gate_skips_below_level_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 100)
    monkeypatch.setattr(f"{_RUNNER}.settings.phase2_seniority_gate_enabled", True)
    monkeypatch.setattr(f"{_RUNNER}.settings.phase2_seniority_gate_tolerance", 1)
    rows = _gate_rows(["j-dir", "j-mgr", "j-coord", "j-eng"])
    jobs = [
        {"id": "j-dir", "title": "Director of CX Operations", "description_html": ""},
        {"id": "j-mgr", "title": "Customer Success Manager", "description_html": ""},
        {"id": "j-coord", "title": "CX Coordinator", "description_html": ""},
        {"id": "j-eng", "title": "Senior Sales Engineer", "description_html": ""},
    ]
    n = await run_phase2_for_jobs(
        _supabase(rows),
        MagicMock(),
        target=_director_target(),
        payload=_payload(),
        jobs=jobs,
    )
    # Director + (tolerance=1) Manager grade; Coordinator + senior-IC dropped.
    assert n == 2
    assert set(graded) == {"j-dir", "j-mgr"}


@pytest.mark.asyncio
async def test_seniority_gate_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off (default) → every promising candidate still grades."""
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 100)
    rows = _gate_rows(["j-dir", "j-coord"])
    jobs = [
        {"id": "j-dir", "title": "Director of CX", "description_html": ""},
        {"id": "j-coord", "title": "CX Coordinator", "description_html": ""},
    ]
    n = await run_phase2_for_jobs(
        _supabase(rows),
        MagicMock(),
        target=_director_target(),
        payload=_payload(),
        jobs=jobs,
    )
    assert n == 2
    assert set(graded) == {"j-dir", "j-coord"}


# ---- pre-scan cosine gate (#90 flip) ---------------------------------------


def _prom_rows(ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "job_posting_id": j,
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
        }
        for j in ids
    ]


def _prom_jobs(ids: list[str]) -> list[dict[str, Any]]:
    return [{"id": j, "title": "x", "description_html": ""} for j in ids]




@pytest.mark.asyncio
async def test_us_gate_skips_confirmed_non_us_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#60: a CONFIRMED non-US job (is_us=False) is never (re-)graded — the write
    side of the display read gate. US (True) and not-yet-tagged (None) jobs still
    grade, so a job's FIRST grade (before the tagger runs) is unaffected; only
    re-grades of confirmed-non-US jobs are dropped (they burned a fresh LLM grade
    every cycle since archived_at doesn't gate grading)."""
    ids = ["j-us", "j-nonus", "j-untagged"]
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 100)

    jobs = [
        {"id": "j-us", "title": "x", "description_html": "", "is_us": True},
        {"id": "j-nonus", "title": "x", "description_html": "", "is_us": False},
        {"id": "j-untagged", "title": "x", "description_html": "", "is_us": None},
    ]
    n = await run_phase2_for_jobs(
        _supabase(_prom_rows(ids)),
        MagicMock(),
        target=_target(1),
        payload=_payload(),
        jobs=jobs,
    )
    assert set(graded) == {"j-us", "j-untagged"}  # confirmed non-US dropped
    assert "j-nonus" not in graded
    assert n == 2


# ---- family gate (#277/#278 write side) ------------------------------------


async def test_family_gate_never_grades_hard_offfamily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A customer_experience listing must never burn a grade against an
    engineering-family target (2026-07-30 prod audit: 55% of promising rows
    were hard off-family, each graded one pure waste). Untagged jobs keep the
    benefit of the doubt — the tagger is async and may not have landed."""
    ids = ["j-eng", "j-cx", "j-untagged"]
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 100)

    target = _target(1).model_copy(update={"role_family": "engineering"})
    jobs = [
        {"id": "j-eng", "title": "x", "description_html": "", "role_family": "engineering"},
        {"id": "j-cx", "title": "x", "description_html": "", "role_family": "customer_experience"},
        {"id": "j-untagged", "title": "x", "description_html": "", "role_family": None},
    ]
    n = await run_phase2_for_jobs(
        _supabase(_prom_rows(ids)),
        MagicMock(),
        target=target,
        payload=_payload(),
        jobs=jobs,
    )
    assert set(graded) == {"j-eng", "j-untagged"}  # keep-null admits untagged
    assert "j-cx" not in graded
    assert n == 2


async def test_lazy_vectors_ensured_for_exactly_the_candidate_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disk IO slim-down contract: embeddings materialize at grade time for
    exactly the candidates about to be read — there is no embed-on-ingest,
    so this call is the only thing standing between a fresh job and a
    fail-open cosine. Also pins that the flag gates it."""
    from app.config import settings as live_settings

    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 100)
    ensured: list[list[str]] = []

    async def fake_ensure(_sb: Any, _client: Any, jobs: list[dict[str, Any]], **_k: Any) -> dict:
        ensured.append([j["id"] for j in jobs])
        return {}

    monkeypatch.setattr(f"{_RUNNER}.ensure_job_vectors", fake_ensure)
    monkeypatch.setattr(f"{_RUNNER}.get_embeddings_client", lambda: MagicMock())
    monkeypatch.setattr(live_settings, "prescan_embed_enabled", True)

    jobs = [
        {"id": "j-cand", "title": "x", "description_html": ""},
        {"id": "j-not-promising", "title": "x", "description_html": ""},
    ]
    rows = [
        {
            "job_posting_id": "j-cand",
            "promising": True,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
        },
        {
            "job_posting_id": "j-not-promising",
            "promising": False,
            "scoring_status": "stage2",
            "scored_profile_version": 1,
        },
    ]
    await run_phase2_for_jobs(
        _supabase(rows), MagicMock(), target=_target(1), payload=_payload(), jobs=jobs
    )

    assert ensured == [["j-cand"]]  # candidates only — never the whole page
    assert graded == ["j-cand"]


async def test_lazy_vectors_skipped_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as live_settings

    _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 100)
    called: list[Any] = []

    async def fake_ensure(*a: Any, **k: Any) -> dict:
        called.append(a)
        return {}

    monkeypatch.setattr(f"{_RUNNER}.ensure_job_vectors", fake_ensure)
    monkeypatch.setattr(live_settings, "prescan_embed_enabled", False)

    await run_phase2_for_jobs(
        _supabase(_prom_rows(["j1"])),
        MagicMock(),
        target=_target(1),
        payload=_payload(),
        jobs=[{"id": "j1", "title": "x", "description_html": ""}],
    )
    assert called == []


async def test_family_gate_unclassified_target_is_ungated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target with role_family NULL accepts any job family (#277 keep-null,
    target side) — the gate must be a no-op, exactly like the read gates."""
    graded = _patch_grader(monkeypatch)
    _patch_quota(monkeypatch, 100)
    jobs = [
        {"id": "j-cx", "title": "x", "description_html": "", "role_family": "customer_experience"},
    ]
    n = await run_phase2_for_jobs(
        _supabase(_prom_rows(["j-cx"])),
        MagicMock(),
        target=_target(1),  # role_family defaults to None
        payload=_payload(),
        jobs=jobs,
    )
    assert graded == ["j-cx"]
    assert n == 1
