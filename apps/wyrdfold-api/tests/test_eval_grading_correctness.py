"""#193: the grading correctness eval scores Phase-2 fit against a fixed golden
set of grossly-unambiguous (target, resume, job) triples — a warehouse job vs a
senior-frontend target must land LOW, a matching job HIGH. These exercise the
pure band metric (no LLM / no key) and guard the golden fixture: it deserializes
into the real JobTarget / OptimizedPayload models, and has both high + low
extreme cases.
"""

from __future__ import annotations

import json

from scripts.eval_grading_correctness import (
    GOLDEN_PATH,
    _deserialize_target,
    _in_band,
    _load_golden,
    _report,
)

# ---------------------------------------------------------------------------
# Pure metrics (no LLM)
# ---------------------------------------------------------------------------


def test_in_band_is_inclusive_and_none_safe() -> None:
    assert _in_band(50, 100, 50) is True
    assert _in_band(50, 100, 100) is True
    assert _in_band(50, 100, 49) is False
    assert _in_band(0, 25, None) is False


def test_report_scores_bands_and_names_misses() -> None:
    fixture = {"bands": {"high": [50, 100], "low": [0, 25]}}
    cases = [
        {"name": "a", "expected_band": "high"},
        {"name": "b", "expected_band": "low"},
        {"name": "c", "expected_band": "high"},
    ]
    # a: 70 in high (hit); b: 40 NOT in low (miss); c: ungraded (skipped).
    rep = _report(fixture, cases, [70, 40, None])
    assert rep["scored"] == 2
    assert rep["skipped"] == 1
    assert rep["band_accuracy"] == 0.5
    assert [m["name"] for m in rep["misses"]] == ["b"]
    assert rep["misses"][0]["score"] == 40


def test_report_perfect() -> None:
    fixture = {"bands": {"high": [50, 100], "low": [0, 25]}}
    cases = [{"name": "a", "expected_band": "high"}, {"name": "b", "expected_band": "low"}]
    rep = _report(fixture, cases, [80, 10])
    assert rep["band_accuracy"] == 1.0
    assert rep["misses"] == []


# ---------------------------------------------------------------------------
# Golden fixture guards
# ---------------------------------------------------------------------------


def _fixture() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def test_golden_is_wellformed_and_deserializes() -> None:
    fx = _fixture()
    bands = fx["bands"]
    assert set(bands) >= {"high", "low"}
    for lo, hi in bands.values():
        assert lo <= hi
    # Every target bank deserializes into the REAL models the grader consumes.
    for entry in fx["targets"].values():
        target, payload = _deserialize_target(entry)
        assert target.scoring_profile is not None
        assert payload.roles  # a resume with at least one role
    cases = _load_golden(fx)
    assert len(cases) >= 4
    for c in cases:
        assert c["expected_band"] in bands
        assert c["target_key"] in fx["targets"]
        assert c.get("title") and c.get("jd_text")


def test_golden_has_both_extreme_bands() -> None:
    cases = _load_golden(_fixture())
    bands_used = {c["expected_band"] for c in cases}
    assert "high" in bands_used and "low" in bands_used
    # The motivating gross case: an off-domain job vs a specific target.
    assert any(c["expected_band"] == "low" for c in cases)
