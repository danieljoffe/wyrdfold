"""Dictionary-based catalog skill extraction (free, no LLM).

What these protect:

- WORD BOUNDARIES. The whole approach rests on "the skill name literally
  appears in the posting", so a substring false positive ("java" inside
  "javascript") would put a wrong facet on a job — and because search matches
  the stored string exactly, a wrong facet is a wrong result set.
- ALIAS CANONICALIZATION. ``k8s``/``postgres``/``golang`` must land on the
  same key the search filter queries with. This is the property the LLM lacked
  (its output fragmented into 1,757 values, 68% singletons).
- THE CATEGORY BAN. Field-wide words ("ai", "cloud", "engineering") must stay
  OUT of the vocabulary: they match nearly every posting in a discipline, so
  they cannot narrow a search.
- READ/WRITE AGREEMENT with ``job_search.normalize_skill_filter`` — a query
  for "React" must normalize onto a key this module can actually emit.
"""

from __future__ import annotations

import pytest

from app.services.qualification.skill_dictionary import (
    MAX_SKILLS_PER_JOB,
    SKILL_DICTIONARY,
    VOCABULARY,
    extract_skills,
    unknown_terms,
)

_JD = "<p>You'll build with React, TypeScript and Postgres on k8s.</p>"


# ---- matching ---------------------------------------------------------------


def test_extracts_canonical_names_from_html() -> None:
    assert extract_skills("Senior Frontend Engineer", _JD) == [
        "kubernetes",
        "postgresql",
        "react",
        "typescript",
    ]


def test_aliases_collapse_onto_one_canonical_key() -> None:
    """The property the LLM could not hold: one concept, one facet value."""
    for alias, canon in (
        ("We run k8s in prod", "kubernetes"),
        ("Postgres experience required", "postgresql"),
        ("Golang services", "go"),
        ("PostgreSQL and MySQL", "postgresql"),
        ("built with Next.js", "next.js"),
        ("WCAG compliance", "accessibility"),
    ):
        assert canon in extract_skills(None, alias), alias


def test_title_alone_is_enough() -> None:
    """Many postings name the stack only in the title."""
    assert "kubernetes" in extract_skills("Kubernetes Platform Engineer", None)


@pytest.mark.parametrize(
    ("text", "must_not"),
    [
        # The classic substring trap: java must not fire on javascript.
        ("Strong JavaScript skills", "java"),
        # nor on a longer word that merely starts the same way.
        ("We use Rustling internal tooling", "rust"),
        ("Experience with Goa framework", "go"),
        ("sapphire database", "sap"),
        # "go" is alias-only precisely because bare prose is full of it.
        ("Willingness to go above and beyond", "go"),
    ],
)
def test_word_boundaries_prevent_false_facets(text: str, must_not: str) -> None:
    assert must_not not in extract_skills(None, text)


@pytest.mark.parametrize(
    ("text", "must_not"),
    [
        # Regression: multi-form entries. An UNGROUPED alternation
        # (`(?<!x)sem|paid search(?!y)`) leaves every form but the first
        # unguarded at the start and every form but the last unguarded at the
        # end — so `sem` matched inside "semiconductor" on live postings while
        # every single-form boundary test above still passed. Each case here
        # is a multi-form entry checked against a longer word.
        ("Scanning electron microscopy of semiconductor wafers", "sem"),
        ("ecmascripting conventions", "javascript"),
        ("sapphire glass supplier", "sap"),
        ("mongodbatlas internal tool", "mongodb"),
    ],
)
def test_multiform_entries_are_boundary_guarded(text: str, must_not: str) -> None:
    assert must_not not in extract_skills(None, text)


def test_hyphenated_mentions_still_count() -> None:
    """A hyphen is not alphanumeric, so "postgresql-compatible" matches — and
    should: the posting is genuinely about PostgreSQL. The boundary guards
    exist to stop matches INSIDE a longer word, not to require whitespace."""
    assert "postgresql" in extract_skills(None, "postgresql-compatible storage")
    assert "kubernetes" in extract_skills(None, "kubernetes-native platform")


def test_punctuated_names_still_match() -> None:
    """``c++`` / ``c#`` / ``ci/cd`` end in non-word characters, where a naive
    ``\\b`` behaves backwards — these are the reason for custom lookarounds."""
    got = extract_skills(None, "Deep C++ and C# work, plus CI/CD ownership and .NET")
    assert {"c++", "c#", "ci/cd", ".net"} <= set(got)


def test_no_skills_returns_empty_not_junk() -> None:
    assert extract_skills("Barista", "<p>Make coffee. Be friendly.</p>") == []
    assert extract_skills(None, None) == []


def test_output_is_capped_and_sorted() -> None:
    """Sorted so an unchanged posting produces an unchanged column (the
    content-hash skip stays meaningful); capped so a stack-listing JD cannot
    write an unbounded blob."""
    everything = " ".join(forms[0] for forms in SKILL_DICTIONARY.values())
    got = extract_skills(None, everything)
    assert got == sorted(got)
    assert len(got) == MAX_SKILLS_PER_JOB


# ---- vocabulary hygiene -----------------------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        "ai",
        "cloud",
        "engineering",
        "software development",
        "automation",
        "infrastructure",
        "analytics",
        "technology",
        "devops",
        "data",
    ],
)
def test_field_wide_categories_stay_out_of_the_vocabulary(category: str) -> None:
    """These matched nearly every posting in the LLM's output ("ai" appeared
    in 118 of 748 jobs, as common as "aws"), which makes them useless as
    facets. Keeping them out is a deliberate, reviewed judgement."""
    assert category not in VOCABULARY


def test_every_canonical_key_is_a_clean_facet_value() -> None:
    for canon in VOCABULARY:
        assert canon == canon.lower().strip()
        assert len(canon.split()) <= 4
        assert len(canon) <= 40


def test_search_filter_normalization_agrees_with_the_vocabulary() -> None:
    """A user typing "React"/"  KUBERNETES " must normalize onto a key this
    module can emit — read and write share one vocabulary or the facet
    silently returns nothing."""
    from app.services.job_search import normalize_skill_filter

    for typed, canon in (
        ("React", "react"),
        ("  KUBERNETES ", "kubernetes"),
        ("Node.js", "node.js"),
    ):
        assert normalize_skill_filter([typed]) == [canon]
        assert canon in VOCABULARY


# ---- growth primitive -------------------------------------------------------


def test_unknown_terms_filters_to_genuine_candidates() -> None:
    got = unknown_terms(["react", "Svelte Kit", "  keycloak ", "react", "kubernetes"])
    assert got == ["svelte kit", "keycloak"]  # knowns dropped, deduped, normalized


def test_unknown_terms_tolerates_junk() -> None:
    assert unknown_terms(None) == []
    assert unknown_terms("not a list") == []
    assert unknown_terms([1, None, ""]) == []
