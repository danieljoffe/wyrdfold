"""Tests for job target Pydantic models (#495)."""

import pytest
from pydantic import ValidationError

from app.models.targets import (
    CategoryProfile,
    DomainProfile,
    JobTarget,
    NegativeProfile,
    ReferenceJDAdd,
    ScoringProfile,
    SeniorityProfile,
    TargetCreate,
    TargetFromUrl,
    TargetReferenceJD,
    TargetUpdate,
)

# ---- ScoringProfile --------------------------------------------------------


def test_scoring_profile_defaults():
    p = ScoringProfile()
    assert p.categories == {}
    assert p.seniority.level is None
    assert p.seniority.signals == []
    assert p.domain.signals == []
    assert p.domain.weight == 0.5
    assert p.negative.keywords == []
    assert p.negative.weight == -10.0


def test_scoring_profile_full():
    p = ScoringProfile(
        categories={
            "core_skills": CategoryProfile(keywords={"React": 3, "TypeScript": 3}, weight=2.0),
            "secondary_skills": CategoryProfile(keywords={"Node.js": 2}, weight=1.0),
        },
        seniority=SeniorityProfile(level="senior", signals=["5+ years", "lead"]),
        domain=DomainProfile(signals=["fintech"], weight=0.5),
        negative=NegativeProfile(keywords=["junior", "intern"], weight=-10),
    )
    assert p.categories["core_skills"].keywords["React"] == 3
    assert p.categories["core_skills"].weight == 2.0
    assert p.seniority.level == "senior"
    assert len(p.negative.keywords) == 2


def test_scoring_profile_round_trip():
    p = ScoringProfile(
        categories={
            "core_skills": CategoryProfile(keywords={"React": 3}, weight=2.0),
        },
        seniority=SeniorityProfile(level="senior", signals=["5+ years"]),
        domain=DomainProfile(signals=["fintech"], weight=0.5),
        negative=NegativeProfile(keywords=["junior"], weight=-10),
    )
    dumped = p.model_dump()
    restored = ScoringProfile.model_validate(dumped)
    assert restored == p


def test_scoring_profile_from_json():
    """Validates the exact JSONB shape stored in the database."""
    raw = {
        "categories": {
            "core_skills": {"keywords": {"React": 3}, "weight": 2.0},
        },
        "seniority": {"level": "senior", "signals": ["5+ years"]},
        "domain": {"signals": ["fintech"], "weight": 0.5},
        "negative": {"keywords": ["junior"], "weight": -10},
    }
    p = ScoringProfile.model_validate(raw)
    assert p.categories["core_skills"].keywords["React"] == 3


def test_category_profile_empty_keywords():
    c = CategoryProfile()
    assert c.keywords == {}
    assert c.weight == 1.0


# ---- Request models --------------------------------------------------------


def test_target_create_minimal():
    t = TargetCreate(label="Senior Frontend Engineer")
    assert t.label == "Senior Frontend Engineer"
    assert t.scoring_profile == ScoringProfile()


def test_target_update_partial():
    u = TargetUpdate(label="Staff Engineer")
    assert u.label == "Staff Engineer"
    assert u.scoring_profile is None
    assert u.is_active is None


# ---- Row models ------------------------------------------------------------


def test_job_target_from_dict():
    raw = {
        "id": "abc-123",
        "label": "Frontend",
        "scoring_profile": {"categories": {}},
        "is_active": True,
        "created_at": "2026-04-24T00:00:00Z",
        "updated_at": "2026-04-24T00:00:00Z",
    }
    t = JobTarget.model_validate(raw)
    assert t.id == "abc-123"
    assert t.scoring_profile.categories == {}
    assert t.profile_version == 1


def test_target_reference_jd_from_dict():
    raw = {
        "id": "ref-1",
        "target_id": "abc-123",
        "jd_url": "https://example.com/job",
        "jd_text": "Senior frontend engineer needed...",
        "extracted_profile": {
            "categories": {"core_skills": {"keywords": {"React": 3}, "weight": 2.0}},
        },
        "created_at": "2026-04-24T00:00:00Z",
    }
    jd = TargetReferenceJD.model_validate(raw)
    assert jd.extracted_profile.categories["core_skills"].keywords["React"] == 3


# ---- Example title pools (Phase 1 LLM triage seed) ------------------------


def test_job_target_defaults_example_title_pools_to_empty():
    """Targets created before the Phase 1 LLM triage migration have NULL
    columns in the DB → JobTarget should fall back to empty lists so
    the Phase 1 grader can run "no examples available" path cleanly.
    """
    raw = {
        "id": "t-1",
        "label": "Director of CX Operations",
        "scoring_profile": {"categories": {}},
        "is_active": True,
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        # NO example_promising_titles, NO example_unpromising_titles.
    }
    t = JobTarget.model_validate(raw)
    assert t.example_promising_titles == []
    assert t.example_unpromising_titles == []


def test_job_target_round_trips_example_title_pools():
    raw = {
        "id": "t-1",
        "label": "Director of CX Operations",
        "scoring_profile": {"categories": {}},
        "is_active": True,
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        "example_promising_titles": [
            "Director of Customer Success",
            "Head of CS Operations",
            "VP Customer Experience",
        ],
        "example_unpromising_titles": [
            "Director of Sales Operations",
            "Marketing Director",
        ],
    }
    t = JobTarget.model_validate(raw)
    assert "Director of Customer Success" in t.example_promising_titles
    assert "Marketing Director" in t.example_unpromising_titles


def test_derived_target_defaults_example_title_pools_to_empty():
    """The LLM-output schema: legacy outputs that pre-date the prompt
    extension still validate (fields default to empty lists). Phase 1
    treats empty pools as "fall back to label-only grading"."""
    from app.models.targets import DerivedTarget

    raw = {
        "scoring_profile": {"categories": {}},
        "search_keywords": ["frontend engineer"],
    }
    d = DerivedTarget.model_validate(raw)
    assert d.example_promising_titles == []
    assert d.example_unpromising_titles == []


def test_derived_target_with_example_title_pools():
    from app.models.targets import DerivedTarget

    raw = {
        "scoring_profile": {"categories": {}},
        "search_keywords": ["frontend engineer"],
        "example_promising_titles": ["Senior Frontend Engineer", "Staff Web Engineer"],
        "example_unpromising_titles": ["Senior Product Designer"],
    }
    d = DerivedTarget.model_validate(raw)
    assert len(d.example_promising_titles) == 2
    assert d.example_unpromising_titles == ["Senior Product Designer"]


def test_derived_target_coerces_out_of_vocab_seniority_hint_to_none():
    """#27 safety net: a seniority_hint outside the closed set must NOT reject
    the whole derived profile — it degrades to None ("no hint")."""
    from app.models.targets import DerivedTarget

    # "principal" is not in SeniorityHint; old behaviour raised ValidationError
    # and discarded the entire derivation.
    d = DerivedTarget.model_validate(
        {"scoring_profile": {"categories": {}}, "seniority_hint": "principal"}
    )
    assert d.seniority_hint is None

    # A valid value (any case) is kept, normalized to lower-case.
    d2 = DerivedTarget.model_validate(
        {"scoring_profile": {"categories": {}}, "seniority_hint": "Staff"}
    )
    assert d2.seniority_hint == "staff"

    # Absent / None stays None.
    d3 = DerivedTarget.model_validate({"scoring_profile": {"categories": {}}})
    assert d3.seniority_hint is None


def test_derived_target_truncates_oversized_description():
    """#27 safety net: a description over the cap is truncated, not rejected
    (verbose leadership roles overshoot the prompt's 80-600 char target)."""
    from app.models.targets import DerivedTarget

    long_desc = ("word " * 250).strip()  # ~1250 chars, over the 800 cap
    assert len(long_desc) > 800
    d = DerivedTarget.model_validate(
        {"scoring_profile": {"categories": {}}, "description": long_desc}
    )
    assert d.description is not None
    assert len(d.description) <= 800
    assert d.description.endswith("…")


def test_target_update_passes_through_example_title_pools():
    """``TargetUpdate`` must carry the new fields through so derive →
    update flows can persist them. Both fields are ``None`` by default
    so unrelated updates don't accidentally null out the pool."""
    from app.models.targets import TargetUpdate

    upd = TargetUpdate(
        example_promising_titles=["Senior Frontend Engineer"],
        example_unpromising_titles=["Senior Product Designer"],
    )
    assert upd.example_promising_titles == ["Senior Frontend Engineer"]
    assert upd.example_unpromising_titles == ["Senior Product Designer"]
    # Unrelated update: pools stay None (= "don't touch").
    upd2 = TargetUpdate(label="Renamed Target")
    assert upd2.example_promising_titles is None
    assert upd2.example_unpromising_titles is None


# ---- jd_url length bound (audit #29) --------------------------------------
#
# ``TargetFromUrl.jd_url`` and ``ReferenceJDAdd.jd_url`` are user-supplied URLs
# that get fetched server-side. They must carry the same 2048-char bound as
# ``ManualJobRequest.url`` / ``UrlValidateRequest.url`` so an oversized URL is
# rejected at the edge (422 via FastAPI) rather than flowing into the fetch
# pipeline. These exercise the model validator directly; a ValidationError here
# is what FastAPI surfaces as a 422.

_MAX_URL = 2048


def _url_of_length(n: int) -> str:
    """A syntactically plausible https URL padded to exactly ``n`` chars."""
    prefix = "https://example.com/"
    return prefix + "a" * (n - len(prefix))


def test_target_from_url_accepts_max_length_url():
    url = _url_of_length(_MAX_URL)
    assert len(url) == _MAX_URL
    t = TargetFromUrl(jd_url=url)
    assert t.jd_url == url


def test_target_from_url_rejects_oversized_url():
    url = _url_of_length(_MAX_URL + 1)
    assert len(url) == _MAX_URL + 1
    with pytest.raises(ValidationError) as exc:
        TargetFromUrl(jd_url=url)
    # The failure is specifically the length bound on jd_url.
    errors = exc.value.errors()
    assert any(
        e["loc"] == ("jd_url",) and e["type"] == "string_too_long" for e in errors
    ), errors


def test_reference_jd_add_accepts_max_length_url():
    url = _url_of_length(_MAX_URL)
    ref = ReferenceJDAdd(jd_url=url)
    assert ref.jd_url == url


def test_reference_jd_add_rejects_oversized_url():
    url = _url_of_length(_MAX_URL + 1)
    with pytest.raises(ValidationError) as exc:
        ReferenceJDAdd(jd_url=url)
    errors = exc.value.errors()
    assert any(
        e["loc"] == ("jd_url",) and e["type"] == "string_too_long" for e in errors
    ), errors


def test_reference_jd_add_still_requires_text_or_url():
    """Negative control: the new length bound must not weaken the existing
    "either jd_text or jd_url" guard. An empty payload still fails, but for
    the missing-field reason — not a length error."""
    with pytest.raises(ValidationError) as exc:
        ReferenceJDAdd()
    msg = str(exc.value)
    assert "Either jd_text or jd_url is required" in msg
