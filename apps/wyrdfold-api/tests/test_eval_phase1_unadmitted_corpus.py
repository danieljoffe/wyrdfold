"""Guards for the unadmitted-stack Phase-1 bake-off corpus and the report
extensions it drives.

Two things are pinned here, both free (no LLM, no network):

1. **The committed corpus fixture** — it is the input to a model-swap decision,
   so its shape has to hold: every case names a real target, every target
   carries the two fields the Phase-1 prompt actually reads, no case carries the
   second-person ``description`` (this repo is public, #868), and the strata
   are both populated (a corpus that is all ``cross_gate`` would be an easy
   off-family quiz, not a triage eval).

2. **The report extensions** — cost-per-1k / backlog projection, and the
   per-stratum split. The split exists because a model can post a great headline
   agreement while being bad at the only pairs that are hard; a test that only
   checked the headline would not notice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.eval_phase1_triage import (
    _agreement_report,
    _apply_admission,
    _parse_confidences,
    _strata_by_key,
    _strata_by_target,
)

_CORPUS = Path(__file__).parent / "fixtures" / "phase1_unadmitted_corpus.json"


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    if not _CORPUS.exists():
        pytest.skip(f"corpus fixture not present: {_CORPUS}")
    return dict(json.loads(_CORPUS.read_text()))


def test_every_case_names_a_target_in_the_fixture(corpus: dict[str, Any]) -> None:
    known = set(corpus["targets"])
    assert known, "corpus carries no targets"
    unknown = {c["target_id"] for c in corpus["cases"]} - known
    assert not unknown, f"cases reference targets that aren't in the fixture: {unknown}"


def test_targets_carry_what_the_phase1_prompt_reads(corpus: dict[str, Any]) -> None:
    # _split_user_message uses label + the two example pools and nothing else.
    for tid, meta in corpus["targets"].items():
        t = meta["target"]
        assert t["label"], f"{tid} has no label — the prompt would grade against nothing"
        assert t["example_promising_titles"], f"{tid} has no promising examples"
        assert t["example_unpromising_titles"], f"{tid} has no unpromising examples"


def test_no_target_description_leaks_into_the_public_fixture(corpus: dict[str, Any]) -> None:
    # targets.description is second-person prose that names employers (#868).
    # The prompt never reads it, so its presence here would be pure leak.
    for tid, meta in corpus["targets"].items():
        assert "description" not in meta["target"], f"{tid} carries a description"


def test_both_strata_are_populated(corpus: dict[str, Any]) -> None:
    strata = {c.get("stratum") for c in corpus["cases"]}
    assert strata == {"own_gate", "cross_gate"}, strata
    own = sum(1 for c in corpus["cases"] if c["stratum"] == "own_gate")
    # A corpus with a token handful of hard pairs cannot separate models on the
    # half that matters. 10% is a floor, not a target.
    assert own >= 0.10 * len(corpus["cases"]), (
        f"only {own}/{len(corpus['cases'])} own-gate pairs — too few hard pairs to rank models"
    )


def test_cases_are_the_target_x_title_cross_product(corpus: dict[str, Any]) -> None:
    # Phase 1 grades every free-gate survivor against every unblocked target;
    # the fixture has to encode that or the projected cost is understated.
    n_targets = len(corpus["targets"])
    n_titles = corpus["meta"]["distinct_titles"]
    assert len(corpus["cases"]) == n_titles * n_targets


def _result(model: str, verdicts: dict[int, bool], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": model,
        "target_id": "t1",
        "chunk_idx": 0,
        "verdicts": verdicts,
        "n_titles": len(verdicts),
        "cost_usd": 0.0,
        "latency_ms": 1,
        "error": None,
    }
    base.update(over)
    return base


def test_strata_map_is_keyed_by_chunk_position() -> None:
    titles_by_target = {"t1": ["a", "b", "c"]}
    strata_by_target = {"t1": {"a": "own_gate", "b": "cross_gate", "c": "own_gate"}}
    keyed = _strata_by_key(titles_by_target, strata_by_target, batch_size=2)
    # batch_size 2 → chunk 0 = [a, b] (ids 1,2), chunk 1 = [c] (id 1).
    assert keyed == {
        ("t1", 0, 1): "own_gate",
        ("t1", 0, 2): "cross_gate",
        ("t1", 1, 1): "own_gate",
    }


def test_strata_by_target_ignores_untagged_fixtures() -> None:
    assert _strata_by_target({"cases": [{"target_id": "t1", "title": "a"}]}) == {}


def test_per_stratum_split_exposes_a_model_that_only_gets_the_easy_half_right() -> None:
    """The headline number can be carried entirely by the easy stratum.

    Reference says PROMISING on both hard pairs and UNPROMISING on the six easy
    ones. The candidate nails every easy reject and misses BOTH hard promising
    titles: 75% headline agreement, but a 100% false-negative rate on exactly
    the pairs the gate exists to decide.
    """
    ref = {1: True, 2: True, 3: False, 4: False, 5: False, 6: False, 7: False, 8: False}
    cand = {1: False, 2: False, 3: False, 4: False, 5: False, 6: False, 7: False, 8: False}
    titles = [f"title-{i}" for i in range(1, 9)]
    strata_by_target = {
        "t1": {
            **dict.fromkeys(titles[:2], "own_gate"),
            **dict.fromkeys(titles[2:], "cross_gate"),
        }
    }
    report = _agreement_report(
        [_result("sonnet-4.6", ref), _result("cheap", cand)],
        titles_by_target={"t1": titles},
        models={"sonnet-4.6": "ref-id", "cheap": "cheap-id"},
        reference="sonnet-4.6",
        strata_by_key=_strata_by_key({"t1": titles}, strata_by_target, batch_size=250),
    )
    stats = report["per_model"]["cheap"]

    assert stats["agreement_rate"] == 0.75  # looks survivable
    per_stratum = stats["per_stratum"]
    assert per_stratum["cross_gate"]["agreement_rate"] == 1.0
    assert per_stratum["own_gate"]["agreement_rate"] == 0.0
    assert per_stratum["own_gate"]["false_negative_rate"] == 1.0
    assert per_stratum["own_gate"]["compared"] == 2
    assert per_stratum["cross_gate"]["compared"] == 6


def test_cost_per_1k_uses_titles_sent_not_verdicts_returned() -> None:
    """You pay for the prompt whether or not the model answers.

    The candidate is SENT 100 titles and returns 2 verdicts. Billing per verdict
    would make a model that drops 98% of its work look 50× cheaper than it is.
    """
    results = [
        _result("sonnet-4.6", {1: True, 2: False}, n_titles=100, cost_usd=1.0),
        _result("cheap", {1: True, 2: False}, n_titles=100, cost_usd=0.5),
    ]
    report = _agreement_report(
        results,
        titles_by_target={"t1": ["a", "b"]},
        models={"sonnet-4.6": "ref-id", "cheap": "cheap-id"},
        reference="sonnet-4.6",
        projection_postings=1000,
        projection_targets=2,
    )
    cheap = report["per_model"]["cheap"]
    assert cheap["titles_sent"] == 100
    assert cheap["cost_per_1k_titles_usd"] == 5.0  # $0.50 / 100 titles x 1000
    # 1,000 postings x 2 targets = 2,000 pairs x $5/1k = $10.
    assert report["projection"] == {"postings": 1000, "targets": 2, "pairs": 2000}
    assert cheap["projected_backlog_usd"] == 10.0
    # The reference is quoted too, so the write-up can price the oracle.
    assert report["per_model"]["sonnet-4.6"]["cost_per_1k_titles_usd"] == 10.0


def test_confidence_parsing_rejects_bools_and_out_of_range() -> None:
    raw = {
        "verdicts": [
            {"id": 1, "promising": True, "confidence": 95},
            {"id": 2, "promising": True, "confidence": True},  # bool is an int subclass
            {"id": 3, "promising": True, "confidence": 120},  # out of the 0-100 scale
            {"id": 4, "promising": True, "confidence": "high"},
            {"id": 5, "promising": True},  # legacy: no confidence at all
            {"id": 6, "promising": False, "confidence": 0},  # 0 is valid, not falsy-dropped
        ]
    }
    assert _parse_confidences(raw) == {1: 95, 6: 0}


def test_admission_replay_drops_low_confidence_promising_calls() -> None:
    """A hedged PROMISING is a DROP in production, and the eval has to see it.

    id 1 is a confident promising (admitted), id 2 a hedged one below the floor
    (dropped — invisible to a raw-verdict eval), id 3 a legacy verdict with no
    confidence (fail-open, still admitted), id 4 an unpromising call.
    """
    results = [
        {
            "model": "cheap",
            "target_id": "t1",
            "chunk_idx": 0,
            "verdicts": {1: True, 2: True, 3: True, 4: False},
            "confidences": {1: 90, 2: 20, 4: 95},
            "n_titles": 4,
            "cost_usd": 0.0,
            "latency_ms": 1,
            "error": None,
        }
    ]
    gated = _apply_admission(results, min_confidence=40)
    assert gated[0]["verdicts"] == {1: True, 2: False, 3: True, 4: False}
    # Non-destructive: the raw run is still available for the raw report.
    assert results[0]["verdicts"] == {1: True, 2: True, 3: True, 4: False}


def test_admission_replay_turns_a_hedging_model_into_a_false_negative() -> None:
    """The whole point: raw agreement can be perfect while admission is not."""
    ref = {
        "model": "sonnet-4.6",
        "target_id": "t1",
        "chunk_idx": 0,
        "verdicts": {1: True, 2: True},
        "confidences": {1: 95, 2: 92},
        "n_titles": 2,
        "cost_usd": 0.0,
        "latency_ms": 1,
        "error": None,
    }
    hedger = {**ref, "model": "cheap", "confidences": {1: 95, 2: 15}}
    models = {"sonnet-4.6": "ref-id", "cheap": "cheap-id"}

    raw = _agreement_report([ref, hedger], {"t1": ["a", "b"]}, models, reference="sonnet-4.6")
    assert raw["per_model"]["cheap"]["false_negative_rate"] == 0.0

    gated = _agreement_report(
        _apply_admission([ref, hedger], 40), {"t1": ["a", "b"]}, models, reference="sonnet-4.6"
    )
    assert gated["per_model"]["cheap"]["false_negative_rate"] == 0.5


def test_projection_target_count_defaults_to_the_fixture_target_count() -> None:
    results = [
        _result("sonnet-4.6", {1: True}, target_id=t, n_titles=1, cost_usd=0.0)
        for t in ("t1", "t2", "t3")
    ]
    report = _agreement_report(
        results,
        titles_by_target={"t1": ["a"], "t2": ["a"], "t3": ["a"]},
        models={"sonnet-4.6": "ref-id"},
        reference="sonnet-4.6",
        projection_postings=100,
    )
    assert report["projection"]["targets"] == 3
    assert report["projection"]["pairs"] == 300
