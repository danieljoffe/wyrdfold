"""#193: the qualification-tagger correctness eval scores is_us / role_family
against ground-truth labels (not drift vs a past run). These exercise the pure
metric on synthetic predictions (no LLM / no key) and guard the golden fixture —
including the conf-95 false-negative regression case (a US location tagged
non-US). The module's app/LLM imports are deferred inside ``_tag_cases``, so
importing it here needs neither a key nor Supabase.
"""

from __future__ import annotations

import json

from app.services.qualification import positively_us_location
from scripts.eval_qualification_correctness import (
    GOLDEN_PATH,
    _is_us_metrics,
    _load_golden,
    _report,
    _role_family_metrics,
)

_ROLE_FAMILIES = {
    "engineering",
    "data_ml",
    "product",
    "design",
    "customer_experience",
    "operations",
    "sales",
    "finance",
    "marketing",
    "people_hr",
    "legal",
    "other",
}


# ---------------------------------------------------------------------------
# Pure metrics (no LLM)
# ---------------------------------------------------------------------------


def test_is_us_positive_class_makes_a_us_mistag_a_recall_miss() -> None:
    # 2 truly-US (one caught, one tagged non-US = FN), 2 non-US (one caught,
    # one leaked as US = FP). The FN is the conf-95 class.
    pairs = [(True, True), (True, False), (False, False), (False, True)]
    m = _is_us_metrics(pairs)
    assert m["confusion"] == {"tp": 1, "fp": 1, "tn": 1, "fn": 1}
    assert m["recall"] == 0.5  # caught 1 of 2 truly-US
    assert m["precision"] == 0.5
    assert m["false_negative_rate"] == 0.5
    assert m["accuracy"] == 0.5
    assert m["scored"] == 4


def test_is_us_perfect_and_empty() -> None:
    assert _is_us_metrics([(True, True), (False, False)])["accuracy"] == 1.0
    assert _is_us_metrics([]) == {}


def test_role_family_accuracy_and_misses() -> None:
    # Last pair is unlabeled (empty expected) → excluded from scoring.
    pairs = [("engineering", "engineering"), ("sales", "finance"), ("", "product")]
    m = _role_family_metrics(pairs)
    assert m["scored"] == 2
    assert m["accuracy"] == 0.5
    assert m["misses"] == [{"expected": "sales", "predicted": "finance"}]


def test_role_family_empty_without_labels() -> None:
    assert _role_family_metrics([("", "x")]) == {}


# ---------------------------------------------------------------------------
# Report wiring (fake tags, no LLM)
# ---------------------------------------------------------------------------


class _FakeTags:
    def __init__(self, is_us: bool, role_family: str, us_confidence: int = 90) -> None:
        self.is_us = is_us
        self.role_family = role_family
        self.us_confidence = us_confidence


def test_report_names_the_is_us_miss() -> None:
    cases = [
        {
            "title": "React Eng",
            "location": "New York, NY, United States",
            "expected_is_us": True,
            "expected_role_family": "engineering",
        }
    ]
    preds = [_FakeTags(is_us=False, role_family="engineering", us_confidence=95)]  # the FN
    rep = _report(cases, preds)
    assert rep["is_us"]["confusion"]["fn"] == 1
    assert rep["is_us_misses"][0]["title"] == "React Eng"
    assert rep["is_us_misses"][0]["us_confidence"] == 95
    assert rep["role_family"]["accuracy"] == 1.0


def test_report_counts_skipped_untagged() -> None:
    cases = [{"expected_is_us": True}, {"expected_is_us": False}]
    rep = _report(cases, [None, _FakeTags(is_us=False, role_family="sales")])
    assert rep["skipped_untagged"] == 1


# ---------------------------------------------------------------------------
# Golden fixture guards
# ---------------------------------------------------------------------------


def _golden() -> list[dict]:
    return _load_golden(json.loads(GOLDEN_PATH.read_text()))


def test_golden_fixture_is_wellformed() -> None:
    cases = _golden()
    assert len(cases) >= 20
    for c in cases:
        assert c.get("title") and c.get("location")
        if c.get("expected_is_us") is not None:
            assert isinstance(c["expected_is_us"], bool)
        if c.get("expected_role_family"):
            assert c["expected_role_family"] in _ROLE_FAMILIES


def test_golden_contains_the_conf95_false_negative_regression() -> None:
    """The exact prod false-negative stays in the set so the eval always tests it."""
    fn = next((c for c in _golden() if c["location"] == "New York, NY, United States"), None)
    assert fn is not None
    assert fn["expected_is_us"] is True


def test_veto_protects_explicit_us_marker_golden_cases() -> None:
    """#246 hedge: positively_us_location must be True for the explicit-US golden
    cases, so a tagger false-negative can never archive them."""
    marked = [
        c
        for c in _golden()
        if c.get("expected_is_us") and ("United States" in c["location"] or "USA" in c["location"])
    ]
    assert marked  # the fixture has such cases
    for c in marked:
        assert positively_us_location(c["location"]) is True
