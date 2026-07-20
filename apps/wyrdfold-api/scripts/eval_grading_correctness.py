"""#193: grading CORRECTNESS eval — does Phase-2 grade the grossly-obvious cases right?

`eval_grading_prompts.py` measures DRIFT (Spearman ρ vs the shifting production
baseline), so it can't tell you the grader is *correct*, only that it changed.
This scores the grader against a fixed, committed golden set
(`tests/fixtures/grading_golden.json`) of (target, resume, job) triples where the
right fit is UNAMBIGUOUS — a warehouse job vs a senior-frontend target+resume
must score LOW; a matching senior-frontend job must score HIGH — with WIDE bands
(high>=50, low<=25). It catches GROSS regressions (a warehouse role scoring 60
for a frontend target) without over-specifying a subtle score.

Reuses the real grader (`eval_grading_prompts._grade_one` → `job_fit`). The pure
metric functions (`_load_golden` / `_in_band` / `_report`) are unit-tested with
synthetic scores in `tests/test_eval_grading_correctness.py` — no LLM, no key —
so CI guards the scoring + the fixture (that it deserializes into real
JobTarget/OptimizedPayload models). The grader run is on-demand:

    railway run uv run --package wyrdfold-api \
        python apps/wyrdfold-api/scripts/eval_grading_correctness.py

Bands ratified 2026-07-07 against the live grader (Sonnet-4.6): band_accuracy
100% (6/6) — highs scored 72/92/95, lows 2/4/4 (a clean ~68-pt gap). Cost ≈ one
grade per case. READ-ONLY (no DB writes).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "grading_golden.json"


def _load_golden(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Cases carrying an ``expected_band`` label."""
    return [c for c in fixture.get("cases", []) if c.get("expected_band")]


def _in_band(lo: float, hi: float, score: float | None) -> bool:
    """Whether ``score`` falls inside the inclusive ``[lo, hi]`` band."""
    return score is not None and lo <= score <= hi


def _report(
    fixture: dict[str, Any], cases: list[dict[str, Any]], scores: list[int | None]
) -> dict[str, Any]:
    """Band-hit accuracy + the concrete misses (a case whose grader score fell
    OUTSIDE its expected band — a grading correctness failure)."""
    bands: dict[str, list[float]] = fixture["bands"]
    rows = []
    for case, score in zip(cases, scores, strict=True):
        lo, hi = bands[case["expected_band"]]
        rows.append(
            {
                "name": case.get("name"),
                "expected_band": case["expected_band"],
                "lo": lo,
                "hi": hi,
                "score": score,
            }
        )
    scored = [r for r in rows if r["score"] is not None]
    misses = [r for r in scored if not _in_band(r["lo"], r["hi"], r["score"])]
    return {
        "band_accuracy": round((len(scored) - len(misses)) / max(1, len(scored)), 4),
        "scored": len(scored),
        "skipped": len(rows) - len(scored),
        "misses": misses,
    }


def _deserialize_target(entry: dict[str, Any]) -> tuple[Any, Any]:
    """A target-bank entry's ``target`` / ``payload`` → real
    ``JobTarget`` / ``OptimizedPayload`` models (validates the fixture)."""
    from app.models.experience import OptimizedPayload
    from app.models.targets import JobTarget

    return (
        JobTarget.model_validate(entry["target"]),
        OptimizedPayload.model_validate(entry["payload"]),
    )


async def _grade_cases(fixture: dict[str, Any], cases: list[dict[str, Any]]) -> list[int | None]:
    """Grade each case with the REAL Phase-2 grader, returning fit_scores
    aligned to ``cases`` (``None`` on failure)."""
    from app.services.fit.job_fit import _SYSTEM_PROMPT
    from app.services.llm import get_default_client as get_llm
    from scripts.eval_grading_prompts import _grade_one

    # No Supabase needed: the cases are self-contained and get_default_client
    # reads only the LLM provider + key. So this runs with just an LLM key
    # (railway run, or LLM_PROVIDER=openrouter + OPENROUTER_API_KEY).
    banks = {k: _deserialize_target(v) for k, v in fixture["targets"].items()}
    llm = get_llm()

    async def _one(case: dict[str, Any]) -> int | None:
        try:
            target, payload = banks[case["target_key"]]
            fit, _tin, _tout, _cost = await _grade_one(
                llm,
                model="claude-sonnet-4-6",
                system_prompt=_SYSTEM_PROMPT,
                payload=payload,
                target=target,
                title=case["title"],
                jd_text=case["jd_text"],
                max_tokens=1024,
            )
            return fit.fit_score if fit else None
        except Exception:
            logger.exception("grading failed for case %s", case.get("name"))
            return None

    return await asyncio.gather(*(_one(c) for c in cases))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    fixture = json.loads(GOLDEN_PATH.read_text())
    cases = _load_golden(fixture)
    logger.info("Grading %d golden cases with the live Phase-2 grader...", len(cases))
    scores = asyncio.run(_grade_cases(fixture, cases))
    for case, score in zip(cases, scores, strict=True):
        logger.info("  %-28s expected=%-4s -> score=%s", case["name"], case["expected_band"], score)
    report = _report(fixture, cases, scores)
    logger.info(
        "grading correctness: band_accuracy=%.1f%% (n=%d, skipped=%d)",
        report["band_accuracy"] * 100,
        report["scored"],
        report["skipped"],
    )
    for m in report["misses"]:
        logger.info(
            "  MISS: %s — expected %s [%g,%g], scored %s",
            m["name"],
            m["expected_band"],
            m["lo"],
            m["hi"],
            m["score"],
        )


if __name__ == "__main__":
    main()
