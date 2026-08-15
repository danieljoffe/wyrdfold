"""Correctness of the title-normalizer eval's detectors (spend-free).

An eval is only worth its spend if its detectors fire. These feed
``analyse_posting`` / ``summarise_group`` the shapes the prompt is supposed to
prevent — the #749 seniority leak above all — and assert each is caught, then
feed clean output and assert none are.

No network, no LLM: strings in, metrics out.
"""

from __future__ import annotations

from typing import Any

from scripts.eval_normalize_posting_title import analyse_posting, summarise_group


def _one(label: str, title: str, *, noise: list[str] | None = None, bare: bool = False):
    return analyse_posting(
        label=label,
        title=title,
        noise_tokens=noise or [],
        expect_no_seniority=bare,
    )


# --- #749: seniority may only come from the TITLE ---------------------------


def test_flags_a_level_word_the_title_never_had() -> None:
    """The exact #749 defect: a bare title whose JD talks about 10+ years and
    staff scope must not gain a level word."""
    got = _one("Senior Software Engineer", "Software Engineer", bare=True)
    assert got["seniority_leak"] == ["senior"]


def test_accepts_a_level_word_the_title_did_have() -> None:
    got = _one("Staff Frontend Engineer", "Staff Frontend Engineer (Remote) - JR-4417")
    assert got["seniority_leak"] == []


def test_expanding_an_abbreviation_is_not_a_leak() -> None:
    """"Sr." -> "Senior" is the canonicalization working, not an invention."""
    got = _one("Senior Product Manager", "Sr. Product Manager (Remote, US) — Platform")
    assert got["seniority_leak"] == []


def test_bare_title_with_no_level_word_is_clean() -> None:
    got = _one("Software Engineer", "Software Engineer", bare=True)
    assert got["seniority_leak"] == []


# --- noise the prompt is told to strip --------------------------------------


def test_flags_surviving_company_and_req_noise() -> None:
    got = _one(
        "Senior Product Manager, Helios Growth - R2938",
        "Senior Product Manager, Helios Growth Team - R2938",
        noise=["Helios", "R2938", "Wayfarer"],
    )
    assert got["noise_survivors"] == ["Helios", "R2938"]  # Wayfarer absent, not flagged


def test_noise_matching_is_case_insensitive() -> None:
    got = _one("Backend Developer rockstar", "Rockstar Backend Developer", noise=["Rockstar"])
    assert got["noise_survivors"] == ["Rockstar"]


def test_clean_label_survives_every_check() -> None:
    got = _one(
        "Senior Product Manager",
        "Senior Product Builder (Product Manager), Enterprise Readiness",
        noise=["Enterprise Readiness", "Wayfarer"],
    )
    assert got["seniority_leak"] == []
    assert got["noise_survivors"] == []
    assert got["over_length"] is False
    assert got["emptied"] is False


# --- the "strip everything" failure mode ------------------------------------


def test_flags_a_label_stripped_down_to_nothing() -> None:
    """Removing all the noise down to one bare word would score perfectly on
    every other metric and be useless as a role label."""
    assert _one("Engineer", "Staff Frontend Engineer (Remote)")["emptied"] is True
    assert _one("Staff Frontend Engineer", "Staff Frontend Engineer")["emptied"] is False


def test_flags_an_over_length_label() -> None:
    long_label = "Senior Product Builder Product Manager Enterprise Readiness and Admin Platform Team"
    assert _one(long_label, long_label)["over_length"] is True


# --- convergence: the property dedup actually depends on --------------------


def _group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return summarise_group({"id": "g"}, rows)


def test_convergence_counts_distinct_dedup_keys_not_strings() -> None:
    """Whitespace and case differences are erased by `normalize_label`, which
    IS the unique key — so they must not read as a split."""
    rows = [
        _one("Senior Product Manager", "a"),
        _one("senior  product manager ", "b"),
    ]
    assert _group(rows)["distinct_labels"] == 1


def test_a_genuine_split_is_reported() -> None:
    """Two catalog rows for one role — the defect #745 exists to prevent."""
    rows = [
        _one("Senior Product Manager", "a"),
        _one("Senior Product Manager, Admin Platform", "b"),
    ]
    s = _group(rows)
    assert s["distinct_labels"] == 2
    assert len(s["labels"]) == 2


def test_group_totals_add_up_across_postings() -> None:
    rows = [
        _one("Senior Software Engineer", "Software Engineer", bare=True),
        _one("Engineer", "Software Engineer", bare=True),
    ]
    s = _group(rows)
    assert s["seniority_leaks"] == 1
    assert s["emptied"] == 1
    assert s["postings"] == 2
