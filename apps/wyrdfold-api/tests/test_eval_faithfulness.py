"""#193: the faithfulness eval scores the tailor's anti-hallucination judge
(review_resume_faithfulness) against ground truth — does it CATCH planted
fabrications without over-flagging faithful resumes? These exercise the pure
metric on synthetic verdicts (no LLM / no key) and guard the golden fixture:
it deserializes into the real OptimizedPayload / TailoredResume models, is
balanced across faithful + all three hallucination issue types, and each
planted hallucination actually appears in its resume.
"""

from __future__ import annotations

import json

from scripts.eval_faithfulness import (
    GOLDEN_PATH,
    _deserialize_case,
    _faithfulness_metrics,
    _load_golden,
    _report,
)

_ISSUE_TYPES = {"fabrication", "exaggeration", "unsupported_skill"}


# ---------------------------------------------------------------------------
# Pure metrics (no LLM) — positive class = "has a hallucination"
# ---------------------------------------------------------------------------


def test_missed_hallucination_is_a_recall_miss() -> None:
    # (has_hallucination, judge_flagged): caught, MISSED, over-flagged, clean.
    pairs = [(True, True), (True, False), (False, True), (False, False)]
    m = _faithfulness_metrics(pairs)
    assert m["confusion"] == {"tp": 1, "fp": 1, "tn": 1, "fn": 1}
    assert m["catch_rate"] == 0.5  # caught 1 of 2 hallucinations
    assert m["miss_rate"] == 0.5  # let 1 of 2 through — the dangerous class
    assert m["precision"] == 0.5  # 1 of 2 flags were real
    assert m["accuracy"] == 0.5
    assert m["scored"] == 4


def test_perfect_judge_and_empty() -> None:
    # Catches every hallucination, flags no faithful resume.
    assert _faithfulness_metrics([(True, True), (False, False)])["catch_rate"] == 1.0
    assert _faithfulness_metrics([(True, True), (False, False)])["miss_rate"] == 0.0
    assert _faithfulness_metrics([]) == {}


# ---------------------------------------------------------------------------
# Report wiring (fake reviews, no LLM)
# ---------------------------------------------------------------------------


class _Flag:
    def __init__(self, claim: str) -> None:
        self.claim = claim


class _FakeReview:
    def __init__(self, actionable: list[str]) -> None:
        self._actionable = [_Flag(c) for c in actionable]

    def actionable_flags(self) -> list[_Flag]:
        return self._actionable


def test_report_names_missed_hallucination_and_false_flag() -> None:
    cases = [
        {
            "name": "planted",
            "expected_faithful": False,
            "planted": {"issue": "fabrication", "claim": "42%"},
        },
        {"name": "clean", "expected_faithful": True},
    ]
    # Judge MISSES the planted one (no flags) and wrongly flags the faithful one.
    reviews = [_FakeReview([]), _FakeReview(["some faithful line"])]
    rep = _report(cases, reviews)
    assert rep["metrics"]["confusion"] == {"tp": 0, "fp": 1, "tn": 0, "fn": 1}
    assert rep["missed_hallucinations"] == [
        {"name": "planted", "planted": {"issue": "fabrication", "claim": "42%"}}
    ]
    assert rep["false_flags"][0]["name"] == "clean"


def test_report_counts_skipped() -> None:
    cases = [{"name": "a", "expected_faithful": False}, {"name": "b", "expected_faithful": True}]
    rep = _report(cases, [None, _FakeReview([])])
    assert rep["skipped"] == 1
    assert rep["metrics"]["scored"] == 1


# ---------------------------------------------------------------------------
# Golden fixture guards
# ---------------------------------------------------------------------------


def _golden() -> list[dict]:
    return _load_golden(json.loads(GOLDEN_PATH.read_text()))


def _resume_text(case: dict) -> str:
    r = case["resume"]
    parts = list(r.get("skills", []))
    for role in r.get("experience", []):
        for b in role.get("bullets", []):
            parts.append(b["text"])
    return " ".join(parts).lower()


def test_golden_is_wellformed_and_deserializes() -> None:
    cases = _golden()
    assert len(cases) >= 6
    for c in cases:
        assert c.get("name") and isinstance(c["expected_faithful"], bool)
        # Deserializes into the REAL models the judge consumes (validates the fixture).
        optimized, resume = _deserialize_case(c)
        assert resume.experience  # a resume with at least one role
        if not c["expected_faithful"]:
            assert c["planted"]["issue"] in _ISSUE_TYPES


def test_golden_planted_hallucination_is_present_in_the_resume() -> None:
    """Fixture integrity: the planted claim must actually appear in the resume,
    else the eval would 'pass' by testing a hallucination that isn't there."""
    for c in _golden():
        if not c["expected_faithful"]:
            assert c["planted"]["claim"].lower() in _resume_text(c), c["name"]


def test_golden_is_balanced_across_faithful_and_all_issue_types() -> None:
    cases = _golden()
    faithful = [c for c in cases if c["expected_faithful"]]
    planted = {c["planted"]["issue"] for c in cases if not c["expected_faithful"]}
    assert len(faithful) >= 3  # precision cases (must NOT be flagged)
    assert planted == _ISSUE_TYPES  # every hallucination class is exercised
