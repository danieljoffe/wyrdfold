"""#193: faithfulness eval — does the tailor's anti-hallucination guard actually
catch fabrications?

The product promise is "traced to your experience, never hallucinated." A
runtime review pass (`review_resume_faithfulness`, #6b/#121) flags claims the
source doesn't support and triggers one corrective regen — but nothing measured
whether that judge actually CATCHES hallucinations. This scores it against a
hand-labeled golden set (`tests/fixtures/faithfulness_golden.json`): resumes
with planted fabrication / exaggeration / unsupported_skill vs faithful resumes
(grounded, rephrased, grounded-metric) the judge must NOT flag.

Positive class = "has a hallucination", so **recall is the catch rate** and a
false NEGATIVE is a MISSED fabrication (the dangerous class); precision measures
over-flagging (needless costly regens). "Flagged" = the review has ≥1
*actionable* (medium/high) flag — the same bar the runtime guard acts on.

Pure metric functions (`_load_golden` / `_faithfulness_metrics` / `_report`) are
unit-tested with synthetic verdicts in `tests/test_eval_faithfulness.py` — no
LLM, no key — so CI guards the scoring + the fixture. `main()` runs the REAL
judge; run it with the prod env:

    railway run uv run --package wyrdfold-api \
        python apps/wyrdfold-api/scripts/eval_faithfulness.py

Cost ≈ one review call per case (~a few cents). READ-ONLY (no DB writes).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "faithfulness_golden.json"
)


def _load_golden(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Cases carrying the ``expected_faithful`` label (all of them, for this
    fixture — a real-data snapshot without labels would be skipped)."""
    return [c for c in fixture.get("cases", []) if c.get("expected_faithful") is not None]


def _faithfulness_metrics(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """Binary correctness over (has_hallucination, judge_flagged) pairs.

    Positive class = "has a hallucination", so ``fn`` counts MISSED
    hallucinations (the judge failed to flag a planted fabrication — the
    dangerous class), ``recall`` is the catch rate, and ``fp`` counts faithful
    resumes wrongly flagged (over-flagging → needless regens). ``{}`` for none.
    """
    if not pairs:
        return {}
    tp = sum(1 for h, f in pairs if h and f)
    fn = sum(1 for h, f in pairs if h and not f)
    fp = sum(1 for h, f in pairs if not h and f)
    tn = sum(1 for h, f in pairs if not h and not f)
    n = tp + fn + fp + tn
    return {
        "catch_rate": round(tp / max(1, tp + fn), 4),  # recall of hallucinations
        "precision": round(tp / max(1, tp + fp), 4),
        "miss_rate": round(fn / max(1, tp + fn), 4),  # false-negative rate
        "accuracy": round((tp + tn) / max(1, n), 4),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "scored": n,
    }


def _deserialize_case(case: dict[str, Any]) -> tuple[Any, Any]:
    """Turn a fixture case's ``source`` / ``resume`` dicts into the real
    ``OptimizedPayload`` / ``TailoredResume`` models — so the judge runs on
    exactly the objects the pipeline feeds it (and the fixture is validated)."""
    from app.models.experience import OptimizedPayload
    from app.models.tailor import TailoredResume

    return (
        OptimizedPayload.model_validate(case["source"]),
        TailoredResume.model_validate(case["resume"]),
    )


async def _judge_cases(cases: list[dict[str, Any]]) -> list[Any]:
    """Run the live faithfulness judge over each case, returning the
    ``FaithfulnessReview`` (or ``None`` on failure) aligned to ``cases``."""
    from app.services.llm import get_default_client as get_llm
    from app.services.tailor.faithfulness import review_resume_faithfulness

    # No Supabase needed: get_default_client reads only the LLM provider + key.
    llm = get_llm()

    async def _one(case: dict[str, Any]) -> Any:
        try:
            optimized, resume = _deserialize_case(case)
            review, _result = await review_resume_faithfulness(
                llm, resume=resume, optimized=optimized
            )
            return review
        except Exception:
            logger.exception("faithfulness judge failed for case %s", case.get("name"))
            return None

    return await asyncio.gather(*(_one(c) for c in cases))


def _report(cases: list[dict[str, Any]], reviews: list[Any]) -> dict[str, Any]:
    """Build the catch-rate report + the concrete MISSES (planted hallucinations
    the judge let through) and false-flags (faithful resumes it flagged)."""
    pairs: list[tuple[bool, bool]] = []
    misses: list[dict[str, Any]] = []  # missed hallucinations — the dangerous class
    false_flags: list[dict[str, Any]] = []
    skipped = 0
    for case, review in zip(cases, reviews, strict=True):
        if review is None:
            skipped += 1
            continue
        has_hallucination = not case["expected_faithful"]
        flagged = bool(review.actionable_flags())
        pairs.append((has_hallucination, flagged))
        if has_hallucination and not flagged:
            misses.append({"name": case.get("name"), "planted": case.get("planted")})
        elif not has_hallucination and flagged:
            false_flags.append(
                {
                    "name": case.get("name"),
                    "flags": [f.claim for f in review.actionable_flags()],
                }
            )
    return {
        "metrics": _faithfulness_metrics(pairs),
        "missed_hallucinations": misses,
        "false_flags": false_flags,
        "skipped": skipped,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    fixture = json.loads(GOLDEN_PATH.read_text())
    cases = _load_golden(fixture)
    logger.info("Reviewing %d labeled faithfulness cases with the live judge...", len(cases))
    reviews = asyncio.run(_judge_cases(cases))
    report = _report(cases, reviews)

    m = report["metrics"]
    if m:
        logger.info(
            "faithfulness judge: catch_rate=%.1f%% precision=%.1f%% miss_rate=%.1f%% (n=%d) %s",
            m["catch_rate"] * 100,
            m["precision"] * 100,
            m["miss_rate"] * 100,
            m["scored"],
            m["confusion"],
        )
    for miss in report["missed_hallucinations"]:
        logger.info("  MISSED hallucination: %s — planted %s", miss["name"], miss["planted"])
    for ff in report["false_flags"]:
        logger.info("  false flag (faithful resume flagged): %s — %s", ff["name"], ff["flags"])
    if report["skipped"]:
        logger.info("skipped (judge errored): %d", report["skipped"])


if __name__ == "__main__":
    main()
