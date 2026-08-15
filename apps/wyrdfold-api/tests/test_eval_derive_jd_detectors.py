"""Correctness of the derive-from-JD eval's defect detectors (spend-free).

An eval is only worth its LLM spend if its detectors actually fire. These
tests feed ``_analyse`` the payload shape OBSERVED ON PROD on 2026-08-15
(``docs/ux-resweep-targets-2026-08-14.md`` C2) and assert each defect is
caught, then feed it a clean payload and assert none are. Without this, a
detector that silently matches nothing would report a flawless baseline and
then a flawless "fix".

No network, no DB, no LLM — pure dict in, metrics out.
"""

from __future__ import annotations

from typing import Any

from scripts.eval_derive_profile_from_jd import (
    _analyse,
    _is_perk_noise,
    _is_seniority_evidence,
)


def _payload(
    *,
    categories: dict[str, Any] | None = None,
    domain_signals: list[str] | None = None,
    seniority_signals: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "scoring_profile": {
            "categories": categories
            if categories is not None
            else {"core_skills": {"keywords": {"Go": 3}, "weight": 2.0}},
            "seniority": {
                "level": "senior",
                "signals": seniority_signals if seniority_signals is not None else ["5+ years"],
            },
            "domain": {"signals": domain_signals or [], "weight": 0.5},
            "negative": {"keywords": ["junior"], "weight": -10},
        },
        "search_keywords": ["backend engineer"],
    }


# --- the exact prod defect ---------------------------------------------------


def test_detects_the_prod_cross_section_leak() -> None:
    """ACH / SEPA / PCI-DSS were emitted into BOTH skills and domain."""
    got = _analyse(
        _payload(
            categories={
                "core_skills": {
                    "keywords": {"PCI-DSS": 3, "Rust": 3, "payment rails": 3},
                    "weight": 2.0,
                },
                "secondary_skills": {"keywords": {"ACH": 2, "SEPA": 2}, "weight": 1.0},
            },
            domain_signals=["fintech", "payments", "ACH", "SEPA", "PCI-DSS"],
        )
    )
    assert got["leaked_terms"] == ["ach", "pci-dss", "sepa"]


def test_detects_the_prod_bare_seniority_signals() -> None:
    """`design` / `ship` / `own services` carry no seniority information."""
    got = _analyse(
        _payload(
            seniority_signals=[
                "5+ years",
                "8+ years",
                "senior developer",
                "project leadership",
                "independent decision-making",
                "strategic influence",
                "lead",
                # the offenders
                "design",
                "ship",
                "own services",
                "production backend systems",
            ]
        )
    )
    assert got["bare_signals"] == [
        "design",
        "ship",
        "own services",
        "production backend systems",
    ]


def test_real_seniority_phrasings_are_not_flagged() -> None:
    """Precision guard. The first baseline over-flagged genuine evidence —
    "technical authority", "across three teams", "set technical direction" —
    which would have made any before/after unreadable."""
    got = _analyse(
        _payload(
            seniority_signals=[
                "technical authority",
                "across three teams",
                "set technical direction",
                "own the roadmap",
                "accountable for platform strategy",
                "take initiative",
            ]
        )
    )
    assert got["bare_signals"] == []


def test_detects_perk_boilerplate_in_seniority_signals() -> None:
    """The prompt already bans perks outright, so this one is not a judgement
    call. Observed live: `no on-call rotation` extracted as seniority."""
    got = _analyse(
        _payload(
            seniority_signals=[
                "7+ years",
                "no on-call rotation",
                "we work asynchronously",
                "competitive compensation",
            ]
        )
    )
    assert got["perk_signals"] == [
        "no on-call rotation",
        "we work asynchronously",
        "competitive compensation",
    ]


def test_detects_case_variant_duplicates_within_a_category() -> None:
    got = _analyse(
        _payload(
            categories={
                "core_skills": {
                    "keywords": {"Microservices": 3, "microservices": 2},
                    "weight": 2.0,
                }
            }
        )
    )
    assert len(got["case_dupes"]) == 1
    assert "Microservices" in got["case_dupes"][0]


# --- must NOT fire on clean output ------------------------------------------


def test_clean_extraction_trips_nothing() -> None:
    got = _analyse(
        _payload(
            categories={
                "core_skills": {"keywords": {"Go": 3, "PostgreSQL": 3}, "weight": 2.0},
                "secondary_skills": {"keywords": {"gRPC": 2}, "weight": 1.0},
            },
            domain_signals=["developer-tools", "b2b-saas"],
            seniority_signals=["7+ years", "mentor", "independent architectural decisions"],
        )
    )
    assert got["leaked_terms"] == []
    assert got["bare_signals"] == []
    assert got["case_dupes"] == []


def test_leak_detection_is_case_insensitive() -> None:
    """The extractor is not consistent about casing, so neither is the check."""
    got = _analyse(
        _payload(
            categories={"core_skills": {"keywords": {"Fintech": 3}, "weight": 2.0}},
            domain_signals=["fintech"],
        )
    )
    assert got["leaked_terms"] == ["fintech"]


# --- the volume counters, which stop "emit nothing" scoring as a win --------


def test_volume_counters_expose_an_empty_section() -> None:
    """Zero leaks with zero domain signals is a regression, not a fix — the
    counters are what make that visible in the before/after."""
    got = _analyse(
        _payload(
            categories={"core_skills": {"keywords": {"Go": 3}, "weight": 2.0}},
            domain_signals=[],
            seniority_signals=[],
        )
    )
    assert got["leaked_terms"] == []
    assert got["bare_signals"] == []
    assert got["n_domain_signals"] == 0
    assert got["n_seniority_signals"] == 0
    assert got["n_skill_terms"] == 1


def test_schema_failure_is_reported_not_raised() -> None:
    got = _analyse({"scoring_profile": {"categories": {"core_skills": {"keywords": "nope"}}}})
    assert got["schema_ok"] is False
    assert got["schema_error"]


def test_malformed_payload_does_not_crash_the_run() -> None:
    """A live run returned a whole category as a float and took the other 23
    trials down with it. Bad output is the thing being measured, so it has to
    be survivable."""
    for payload in (
        {"scoring_profile": {"categories": {"core_skills": 2.0}, "domain": 3, "seniority": None}},
        {"scoring_profile": {"categories": {"core_skills": {"keywords": [1, 2]}}}},
        {"scoring_profile": "not-a-dict"},
        {},
    ):
        got = _analyse(payload)
        assert got["schema_ok"] is False
        assert got["leaked_terms"] == []
        assert got["bare_signals"] == []


# --- the seniority-evidence rule itself -------------------------------------


def test_years_phrases_count_as_evidence() -> None:
    for s in ["5+ years", "8 years of experience", "10+ years"]:
        assert _is_seniority_evidence(s), s


def test_scope_and_leadership_markers_count_as_evidence() -> None:
    for s in ["lead", "mentor", "project leadership", "strategic influence", "staff engineer"]:
        assert _is_seniority_evidence(s), s


def test_bare_verbs_do_not_count_as_evidence() -> None:
    for s in ["design", "ship", "build", "own services", "write code", "debug incidents"]:
        assert not _is_seniority_evidence(s), s


def test_own_does_not_satisfy_ownership() -> None:
    """Whole-word matching: `own services` was an observed offender, and a
    substring match against `ownership` would have let it through."""
    assert not _is_seniority_evidence("own services")
    assert _is_seniority_evidence("ownership of the roadmap")


def test_perk_detector_ignores_ordinary_signals() -> None:
    for s in ["5+ years", "lead", "project leadership", "own the roadmap"]:
        assert not _is_perk_noise(s), s
