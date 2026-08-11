"""Unit tests for the coverage-gap analysis (scripts/coverage_gap_report.py).

Pure function, no DB — proves the demand→admission cross-reference that drives
catalog seed priorities (#467).
"""

from __future__ import annotations

from types import SimpleNamespace

from scripts.coverage_gap_report import analyze_coverage_gaps


def _t(label: str, keywords: list[str] | None = None, family: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(label=label, search_keywords=keywords or [], role_family=family)


TARGETS = [_t("Frontend Engineer", ["react", "typescript"], "engineering")]


def test_no_admission_path_zero_result_ranks_first() -> None:
    searches = [
        {"query": "podiatrist", "result_count": 0},
        {"query": "podiatrist", "result_count": 0},
        {"query": "react developer", "result_count": 12},
    ]
    gaps = analyze_coverage_gaps(searches, TARGETS)
    top = gaps[0]
    assert top.query == "podiatrist"
    assert top.has_admission_path is False
    assert top.severity == "zero"
    assert top.n_searches == 2
    assert top.zero_searches == 2


def test_matching_query_has_admission_path_and_is_covered() -> None:
    gaps = analyze_coverage_gaps([{"query": "react", "result_count": 30}], TARGETS)
    assert gaps[0].has_admission_path is True
    assert gaps[0].severity == "covered"


def test_clusters_case_and_whitespace_variants() -> None:
    searches = [
        {"query": "Frontend Engineer", "result_count": 3},
        {"query": "frontend  engineer", "result_count": 1},
    ]
    gaps = analyze_coverage_gaps(searches, TARGETS)
    assert len(gaps) == 1
    assert gaps[0].n_searches == 2
    assert gaps[0].best_results == 3  # highest across the cluster
    assert gaps[0].has_admission_path is True  # "frontend" matches the label


def test_blank_and_null_queries_skipped() -> None:
    gaps = analyze_coverage_gaps(
        [{"query": "", "result_count": 0}, {"query": None, "result_count": 5}],
        TARGETS,
    )
    assert gaps == []


def test_thin_query_with_results_has_demonstrated_path() -> None:
    # 2 results (< threshold) proves the role can be admitted, even though the
    # word "podiatrist" matches no target token — results are proof of a path.
    gaps = analyze_coverage_gaps(
        [{"query": "podiatrist", "result_count": 2}],
        TARGETS,
        thin_threshold=5,
    )
    assert gaps[0].severity == "thin"
    assert gaps[0].has_admission_path is True


def test_zero_result_unmatched_is_a_real_gap() -> None:
    gaps = analyze_coverage_gaps(
        [{"query": "podiatrist", "result_count": 0}],
        TARGETS,
    )
    assert gaps[0].severity == "zero"
    assert gaps[0].has_admission_path is False


def test_zero_result_but_token_match_is_freshness_not_a_gap() -> None:
    # "typescript" is a target keyword: a target admits it, the corpus is just
    # momentarily empty — a poll/freshness issue, not a missing target.
    gaps = analyze_coverage_gaps(
        [{"query": "typescript", "result_count": 0}],
        TARGETS,
    )
    assert gaps[0].severity == "zero"
    assert gaps[0].has_admission_path is True
