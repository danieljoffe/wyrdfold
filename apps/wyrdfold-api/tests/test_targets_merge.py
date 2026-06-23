"""Tests for scoring profile merge logic (#495).

The de-bias-by-contributor tests (#5 refinement layer) live at the bottom.
"""

from datetime import UTC, datetime

from app.models.targets import (
    CategoryProfile,
    DomainProfile,
    NegativeProfile,
    ScoringProfile,
    SeniorityProfile,
    TargetReferenceJD,
)
from app.services.targets.merge import (
    merge_by_contributor,
    merge_profiles,
    merge_reference_jds,
)


def _profile(
    *,
    core: dict[str, int] | None = None,
    core_weight: float = 2.0,
    secondary: dict[str, int] | None = None,
    secondary_weight: float = 1.0,
    seniority_level: str | None = "senior",
    seniority_signals: list[str] | None = None,
    domain_signals: list[str] | None = None,
    domain_weight: float = 0.5,
    negative_keywords: list[str] | None = None,
    negative_weight: float = -10.0,
) -> ScoringProfile:
    """Helper to build profiles concisely."""
    cats: dict[str, CategoryProfile] = {}
    if core is not None:
        cats["core_skills"] = CategoryProfile(keywords=core, weight=core_weight)
    if secondary is not None:
        cats["secondary_skills"] = CategoryProfile(
            keywords=secondary, weight=secondary_weight
        )
    return ScoringProfile(
        categories=cats,
        seniority=SeniorityProfile(
            level=seniority_level, signals=seniority_signals or []
        ),
        domain=DomainProfile(signals=domain_signals or [], weight=domain_weight),
        negative=NegativeProfile(
            keywords=negative_keywords or [], weight=negative_weight
        ),
    )


def test_merge_empty_list():
    result = merge_profiles([])
    assert result == ScoringProfile()


def test_merge_single_profile():
    p = _profile(core={"React": 3, "TypeScript": 3})
    result = merge_profiles([p])
    assert result.categories["core_skills"].keywords == {"React": 3, "TypeScript": 3}
    assert result.categories["core_skills"].weight == 2.0


def test_merge_single_is_deep_copy():
    p = _profile(core={"React": 3})
    result = merge_profiles([p])
    # Mutating the result should not affect the original
    result.categories["core_skills"].keywords["React"] = 99
    assert p.categories["core_skills"].keywords["React"] == 3


def test_merge_two_disjoint_keywords():
    a = _profile(core={"React": 3})
    b = _profile(core={"Vue": 2})
    result = merge_profiles([a, b])
    kw = result.categories["core_skills"].keywords
    assert kw["React"] == 3
    assert kw["Vue"] == 2


def test_merge_overlapping_keywords_averaged():
    a = _profile(core={"React": 3})
    b = _profile(core={"React": 1})
    result = merge_profiles([a, b])
    # avg(3,1) = 2.0, rounded = 2
    assert result.categories["core_skills"].keywords["React"] == 2


def test_merge_overlapping_keywords_min_one():
    a = _profile(core={"Bash": 1})
    b = _profile(core={"Bash": 1})
    result = merge_profiles([a, b])
    # avg(1,1) = 1, rounded = 1, min enforced
    assert result.categories["core_skills"].keywords["Bash"] == 1


def test_merge_category_weights_averaged():
    a = _profile(core={"React": 3}, core_weight=2.0)
    b = _profile(core={"React": 3}, core_weight=1.0)
    result = merge_profiles([a, b])
    assert result.categories["core_skills"].weight == 1.5


def test_merge_disjoint_categories():
    a = _profile(core={"React": 3})
    b = _profile(secondary={"Node.js": 2})
    result = merge_profiles([a, b])
    assert "core_skills" in result.categories
    assert "secondary_skills" in result.categories
    assert result.categories["core_skills"].keywords == {"React": 3}
    assert result.categories["secondary_skills"].keywords == {"Node.js": 2}


def test_merge_seniority_mode():
    a = _profile(seniority_level="senior")
    b = _profile(seniority_level="staff")
    c = _profile(seniority_level="senior")
    result = merge_profiles([a, b, c])
    assert result.seniority.level == "senior"  # mode


def test_merge_seniority_signals_union():
    a = _profile(seniority_signals=["5+ years", "lead"])
    b = _profile(seniority_signals=["lead", "mentor"])
    result = merge_profiles([a, b])
    assert "5+ years" in result.seniority.signals
    assert "lead" in result.seniority.signals
    assert "mentor" in result.seniority.signals
    # No duplicates
    assert len(result.seniority.signals) == 3


def test_merge_domain_signals_union():
    a = _profile(domain_signals=["fintech"])
    b = _profile(domain_signals=["fintech", "b2b-saas"])
    result = merge_profiles([a, b])
    assert "fintech" in result.domain.signals
    assert "b2b-saas" in result.domain.signals
    assert len(result.domain.signals) == 2


def test_merge_domain_weight_averaged():
    a = _profile(domain_weight=0.5)
    b = _profile(domain_weight=1.0)
    result = merge_profiles([a, b])
    assert result.domain.weight == 0.75


def test_merge_negative_keywords_union():
    a = _profile(negative_keywords=["junior"])
    b = _profile(negative_keywords=["junior", "intern"])
    result = merge_profiles([a, b])
    assert "junior" in result.negative.keywords
    assert "intern" in result.negative.keywords
    assert len(result.negative.keywords) == 2


def test_merge_negative_keeps_most_negative_weight():
    a = _profile(negative_weight=-10)
    b = _profile(negative_weight=-15)
    result = merge_profiles([a, b])
    assert result.negative.weight == -15


# ---- De-bias by contributor (#5 refinement layer) --------------------------


def _ref_jd(user_id: str | None, profile: ScoringProfile) -> TargetReferenceJD:
    return TargetReferenceJD(
        id="00000000-0000-0000-0000-000000000000",
        target_id="11111111-1111-1111-1111-111111111111",
        user_id=user_id,
        jd_url=None,
        jd_text="x",
        extracted_profile=profile,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_merge_by_contributor_single_contributor_matches_flat_merge():
    """One contributor's JDs de-bias to the same thing as a flat merge."""
    a = _profile(core={"React": 3}, seniority_level="staff")
    b = _profile(core={"Vue": 1}, seniority_level="senior")
    assert merge_by_contributor([[a, b]]) == merge_profiles([a, b])


def test_merge_by_contributor_prolific_contributor_does_not_dominate_seniority():
    """A user with 3 JDs shouldn't outvote two users with 1 each.

    Flat merge sees levels [staff, staff, staff, senior, senior] -> mode staff.
    De-biased sees one voice per contributor [staff, senior, senior] -> senior.
    """
    prolific = [_profile(seniority_level="staff") for _ in range(3)]
    other1 = _profile(seniority_level="senior")
    other2 = _profile(seniority_level="senior")

    # The prolific user wins the naive (per-JD) merge...
    assert merge_profiles([*prolific, other1, other2]).seniority.level == "staff"
    # ...but de-bias gives each contributor one voice, so the majority wins.
    debiased = merge_by_contributor([prolific, [other1], [other2]])
    assert debiased.seniority.level == "senior"


def test_merge_by_contributor_caps_category_weight_influence():
    prolific = [_profile(core={"React": 3}, core_weight=2.0) for _ in range(3)]
    other = _profile(core={"React": 3}, core_weight=1.0)

    # Flat: avg(2, 2, 2, 1) = 1.75 — pulled toward the prolific contributor.
    assert merge_profiles([*prolific, other]).categories["core_skills"].weight == 1.75
    # De-biased: avg(2, 1) = 1.5 — each contributor counts once.
    debiased = merge_by_contributor([prolific, [other]])
    assert debiased.categories["core_skills"].weight == 1.5


def test_merge_by_contributor_skips_empty_groups():
    a = _profile(core={"React": 3})
    assert merge_by_contributor([[a], []]) == merge_profiles([a])


def test_merge_reference_jds_collapses_null_users_to_one_voice():
    """Legacy/system JDs (NULL user_id) count as a single 'system' contributor."""
    legacy = [_ref_jd(None, _profile(seniority_level="staff")) for _ in range(3)]
    u1 = _ref_jd("user-1", _profile(seniority_level="senior"))
    u2 = _ref_jd("user-2", _profile(seniority_level="senior"))

    # 3 system JDs (one voice) + 2 distinct users -> [staff, senior, senior].
    assert merge_reference_jds([*legacy, u1, u2]).seniority.level == "senior"


def test_merge_reference_jds_empty():
    assert merge_reference_jds([]) == ScoringProfile()


def test_merge_reference_jds_excludes_suppressed_contributions():
    """A down-voted-past-quorum contribution (#5 P3) is dropped from the merge."""
    kept = _ref_jd("user-1", _profile(core={"React": 3}))
    suppressed = _ref_jd("user-2", _profile(core={"Vue": 3}))
    suppressed.suppressed = True

    result = merge_reference_jds([kept, suppressed])
    # Only the kept contributor survives — "Vue" is gone entirely.
    assert result == merge_profiles([kept.extracted_profile])
    assert "Vue" not in result.categories.get("core_skills", CategoryProfile()).keywords


def test_merge_reference_jds_all_suppressed_yields_empty():
    a = _ref_jd("user-1", _profile(core={"React": 3}))
    a.suppressed = True
    assert merge_reference_jds([a]) == ScoringProfile()
