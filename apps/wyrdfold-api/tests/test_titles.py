"""``clean_title_display`` battery (ux-sweep §B1 / title_display design).

Every case here is either observed prod junk or a guard against the cleaner
being too eager — the conservative stance (return ``None``, serve raw) is as
load-bearing as the repairs.
"""

from __future__ import annotations

import pytest

from app.services.titles import clean_title_display


class TestRepairs:
    def test_underscore_artifact_collapses(self) -> None:
        # Observed live: "Project Engineer _field_ Application Engineering".
        assert (
            clean_title_display("Project Engineer _field_ Application Engineering")
            == "Project Engineer field Application Engineering"
        )

    def test_all_lower_title_recases_with_acronyms(self) -> None:
        assert (
            clean_title_display("senior ai software engineer iii")
            == "Senior AI Software Engineer III"
        )

    def test_all_upper_title_recases(self) -> None:
        assert clean_title_display("SENIOR SOFTWARE ENGINEER") == "Senior Software Engineer"

    def test_special_brand_spellings(self) -> None:
        assert clean_title_display("javascript devops engineer") == "JavaScript DevOps Engineer"

    def test_ampersand_splits_runs(self) -> None:
        # "cd&ai" cases per part — the B1 chip mangle "Cd&Ai".
        assert clean_title_display("director, cd&ai") == "Director, CD&AI"

    def test_trailing_req_code_parenthesized(self) -> None:
        assert clean_title_display("Staff Engineer (REQ-20441)") == "Staff Engineer"
        assert clean_title_display("Staff Engineer [R 123456]") == "Staff Engineer"
        # Bare digits need 5+ — a 5-digit id is a code, a 4-digit year is not.
        assert clean_title_display("Staff Engineer (20441)") == "Staff Engineer"

    def test_trailing_req_code_dash(self) -> None:
        assert clean_title_display("Staff Engineer — JR2044123") == "Staff Engineer"

    def test_whitespace_collapse_and_edges(self) -> None:
        assert clean_title_display("  Staff  Engineer -  ") == "Staff Engineer"

    def test_cpp_and_csharp_survive_recasing(self) -> None:
        assert clean_title_display("senior c++ engineer") == "Senior C++ Engineer"


class TestConservatism:
    """The None cases — raw is served untouched."""

    @pytest.mark.parametrize(
        "raw",
        [
            "Senior Software Engineer",
            # Mixed case is deliberate board casing — never re-cased.
            "Make IT Work Specialist",
            "Engineer, iOS Platform",
            # A trailing year is NOT a req code.
            "Software Engineer 2026",
            # A short parenthetical is NOT a req code.
            "Engineer (AHT)",
            # A parenthesized TERM is content — the first prod dry-run
            # flagged this one before the uppercase-prefix rule.
            "Enterprise Technology Intern - Technical Delivery (Fall 2026)",
            # A bare parenthesized year is a year, not a code.
            "Software Engineer (2026)",
        ],
    )
    def test_clean_titles_return_none(self, raw: str) -> None:
        assert clean_title_display(raw) is None

    def test_none_passthrough(self) -> None:
        assert clean_title_display(None) is None

    def test_empty_and_junk_only_return_none(self) -> None:
        assert clean_title_display("") is None
        # All junk collapses to nothing → raw served (garbage either way,
        # but the cleaner must not invent content).
        assert clean_title_display("___") is None
