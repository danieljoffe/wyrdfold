"""#959: the exclusion REASON is recorded, not just the exclusion.

``_title_matches_any_target`` admits negative-keyword rejections into the
catalog specifically so the pipeline can record them "for audit […] a user
asking 'why isn't this in my list?' gets an answer". These tests cover the
half that was missing: storing the answer.

The load-bearing tests here are the negative ones. It is easy to write a
capture that fires on everything and looks green — so each positive case is
paired with a case that must record NOTHING, and the body-vs-title split
(#845) is asserted in both directions.
"""

from app.models.targets import (
    CategoryProfile,
    NegativeProfile,
    ScoringProfile,
    SeniorityProfile,
)
from app.services.scoring import score_job_with_profile, score_title_against_profile
from app.services.target_scoring import _score_row_payload


def _profile(
    *,
    core: dict[str, int] | None = None,
    negative_keywords: list[str] | None = None,
    seniority_level: str | None = None,
) -> ScoringProfile:
    cats: dict[str, CategoryProfile] = {}
    if core is not None:
        cats["core_skills"] = CategoryProfile(keywords=core, weight=2.0)
    return ScoringProfile(
        categories=cats,
        seniority=SeniorityProfile(level=seniority_level, signals=[]),
        negative=NegativeProfile(keywords=negative_keywords or []),
    )


# ---- Stage 1 (title-only) --------------------------------------------------


def test_title_exclusion_records_the_keyword_that_fired():
    profile = _profile(core={"React": 3}, negative_keywords=["junior", "intern"])
    result = score_title_against_profile("Junior React Developer", profile)
    assert result.excluded
    assert result.exclusion_keywords == ["junior"]


def test_title_exclusion_records_every_keyword_that_fired():
    """Two negatives in one title — a single-value field would lose one."""
    profile = _profile(core={"React": 3}, negative_keywords=["junior", "contract"])
    result = score_title_against_profile("Junior Contract React Developer", profile)
    assert result.excluded
    assert sorted(result.exclusion_keywords) == ["contract", "junior"]


def test_title_not_excluded_records_nothing():
    """The guard must stay silent when it does not fire — otherwise every
    'skipped' row would carry a reason and the field would prove nothing."""
    profile = _profile(core={"React": 3}, negative_keywords=["junior"])
    result = score_title_against_profile("Senior React Engineer", profile)
    assert not result.excluded
    assert result.exclusion_keywords == []


def test_title_no_negatives_configured_records_nothing():
    profile = _profile(core={"React": 3})
    result = score_title_against_profile("Junior React Developer", profile)
    assert result.exclusion_keywords == []


# ---- Stage 2 (full JD) -----------------------------------------------------


def test_full_jd_title_exclusion_records_the_keyword():
    profile = _profile(core={"React": 3}, negative_keywords=["intern"])
    result = score_job_with_profile("Intern React Developer", "<p>Build React things.</p>", profile)
    assert result.excluded
    assert result.exclusion_keywords == ["intern"]


def test_full_jd_body_match_is_a_penalty_not_an_exclusion():
    """#845: a negative keyword in the BODY is a soft penalty, never a hard
    exclude. If this ever records a reason, the capture has drifted onto the
    body path and would explain rejections that never happened."""
    profile = _profile(core={"React": 3}, negative_keywords=["intern"])
    result = score_job_with_profile(
        "Staff React Engineer",
        "<p>Requirements: you will mentor our intern cohort.</p>",
        profile,
    )
    assert not result.excluded
    assert result.exclusion_keywords == []


def test_full_jd_body_and_title_records_only_the_title_keyword():
    """Both paths fire on different keywords; only the title one excludes."""
    profile = _profile(core={"React": 3}, negative_keywords=["junior", "intern"])
    result = score_job_with_profile(
        "Junior React Developer",
        "<p>Requirements: work alongside our intern cohort.</p>",
        profile,
    )
    assert result.excluded
    assert result.exclusion_keywords == ["junior"]


# ---- Persistence -----------------------------------------------------------


def _payload(**overrides: object) -> dict:
    from app.models.schemas import ScoreBreakdown

    base = {
        "job_posting_id": "11111111-1111-1111-1111-111111111111",
        "target_id": "22222222-2222-2222-2222-222222222222",
        "score": 0,
        "breakdown": ScoreBreakdown(),
        "matched_keywords": [],
        "excluded": True,
        "exclusion_keywords": ["junior"],
        "scoring_status": "stage1",
    }
    base.update(overrides)
    return _score_row_payload(**base)  # type: ignore[arg-type]


def test_payload_persists_the_keywords():
    assert _payload()["exclusion_keywords"] == ["junior"]


def test_payload_always_writes_the_key_even_when_empty():
    """#928: a bulk upsert writes the UNION of the batch's keys to every row.
    A sometimes-present key would stamp one row's reason onto rows excluded for
    a different reason — or not excluded at all. So the key must be present
    unconditionally, including on the not-excluded rows that share the batch."""
    row = _payload(excluded=False, exclusion_keywords=[])
    assert "exclusion_keywords" in row
    assert row["exclusion_keywords"] == []


def test_payload_key_is_present_on_every_row_of_a_mixed_batch():
    """The #928 failure mode, exercised as a batch rather than asserted about:
    a page mixing excluded and clean rows must have a uniform key set, or the
    union write corrupts the clean rows."""
    batch = [
        _payload(excluded=True, exclusion_keywords=["junior"]),
        _payload(excluded=False, exclusion_keywords=[]),
        _payload(excluded=True, exclusion_keywords=["intern", "contract"]),
    ]
    key_sets = {frozenset(row) for row in batch}
    assert len(key_sets) == 1, "rows disagree on their key set — #928 union hazard"
    assert [row["exclusion_keywords"] for row in batch] == [
        ["junior"],
        [],
        ["intern", "contract"],
    ]
