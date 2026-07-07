"""#193: qualification-tagger CORRECTNESS eval.

Scores the L2 qualification tagger's ``is_us`` / ``role_family`` against a
hand-labeled golden set (``tests/fixtures/qualification_golden.json``) — measuring
correctness vs ground truth, not drift vs a past run. This turns "eyeball the
output" (``eval_qualification.py``) into "score against truth", so the conf-95
false-NEGATIVE class we hit in prod (an unambiguous US location tagged non-US,
e.g. "New York, NY, United States") shows up as a measured **recall** miss
instead of slipping by unseen.

``is_us`` positive class = "is US": a US job tagged non-US is a **false
negative** (recall miss); a non-US job tagged US is a **false positive**.

The pure metric functions (``_load_golden`` / ``_is_us_metrics`` /
``_role_family_metrics``) are unit-tested with synthetic verdicts in
``tests/test_eval_qualification_correctness.py`` — no LLM, no key, so CI guards
the scoring logic + the golden fixture. This script's ``main()`` runs the REAL
tagger over the golden set; run it with the prod env (real LLM key):

    railway run uv run --package wyrdfold-api \
        python apps/wyrdfold-api/scripts/eval_qualification_correctness.py

Cost ≈ one Haiku call per labeled case (~a few cents). READ-ONLY (no DB writes).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qualification_golden.json"
)


def _load_golden(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Cases carrying at least one ground-truth label (``expected_is_us`` or
    ``expected_role_family``). Unlabeled cases are ignored so a real-data
    snapshot stays eyeball-only (mirrors ``eval_phase1_triage._labels_by_target``).
    """
    return [
        c
        for c in fixture.get("cases", [])
        if c.get("expected_is_us") is not None or c.get("expected_role_family")
    ]


def _is_us_metrics(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """Binary correctness for ``is_us`` over (expected, predicted) pairs.

    Positive class = "is US", so ``fn`` counts US jobs tagged non-US (the
    conf-95 class) and ``recall`` is the fraction of truly-US jobs kept as US.
    Returns ``{}`` for no pairs.
    """
    if not pairs:
        return {}
    tp = sum(1 for e, p in pairs if e and p)
    fn = sum(1 for e, p in pairs if e and not p)
    fp = sum(1 for e, p in pairs if not e and p)
    tn = sum(1 for e, p in pairs if not e and not p)
    n = tp + fn + fp + tn
    return {
        "recall": round(tp / max(1, tp + fn), 4),
        "precision": round(tp / max(1, tp + fp), 4),
        "false_negative_rate": round(fn / max(1, tp + fn), 4),
        "accuracy": round((tp + tn) / max(1, n), 4),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "scored": n,
    }


def _role_family_metrics(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Multi-class accuracy for ``role_family`` over labeled (expected,
    predicted) pairs; lists the misses. Returns ``{}`` for no labeled pairs."""
    labeled = [(e, p) for e, p in pairs if e]
    if not labeled:
        return {}
    correct = sum(1 for e, p in labeled if e == p)
    return {
        "accuracy": round(correct / max(1, len(labeled)), 4),
        "scored": len(labeled),
        "misses": [{"expected": e, "predicted": p} for e, p in labeled if e != p],
    }


async def _tag_cases(cases: list[dict[str, Any]]) -> list[Any]:
    """Run the live tagger over each case, returning the ``QualificationTags``
    (or ``None`` on failure) aligned to ``cases``. Uses the instance LLM key +
    prod Supabase, exactly like the poller's target-independent tagging."""
    from app.services.llm import get_client as get_llm_client
    from app.services.qualification import tag_job
    from app.supabase_pool import get_supabase_pool, init_supabase

    init_supabase()
    supabase = get_supabase_pool()
    llm = get_llm_client(supabase, None)

    async def _one(case: dict[str, Any]) -> Any:
        tags, _result = await tag_job(
            llm,
            title=case.get("title", ""),
            company=case.get("company"),
            location=case.get("location"),
            description=case.get("description"),
        )
        return tags

    return await asyncio.gather(*(_one(c) for c in cases))


def _report(cases: list[dict[str, Any]], preds: list[Any]) -> dict[str, Any]:
    """Build the is_us + role_family correctness reports from cases + predictions,
    plus the concrete misses (so a regression names the failing title/location)."""
    is_us_pairs: list[tuple[bool, bool]] = []
    is_us_misses: list[dict[str, Any]] = []
    rf_pairs: list[tuple[str, str]] = []
    skipped = 0
    for case, tags in zip(cases, preds, strict=True):
        if tags is None:
            skipped += 1
            continue
        exp_us = case.get("expected_is_us")
        if exp_us is not None:
            is_us_pairs.append((bool(exp_us), bool(tags.is_us)))
            if bool(exp_us) != bool(tags.is_us):
                is_us_misses.append(
                    {
                        "title": case.get("title"),
                        "location": case.get("location"),
                        "expected_is_us": bool(exp_us),
                        "predicted_is_us": bool(tags.is_us),
                        "us_confidence": tags.us_confidence,
                    }
                )
        rf_pairs.append((case.get("expected_role_family", ""), tags.role_family))
    return {
        "is_us": _is_us_metrics(is_us_pairs),
        "is_us_misses": is_us_misses,
        "role_family": _role_family_metrics(rf_pairs),
        "skipped_untagged": skipped,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    fixture = json.loads(GOLDEN_PATH.read_text())
    cases = _load_golden(fixture)
    logger.info("Tagging %d labeled golden cases with the live qualifier...", len(cases))
    preds = asyncio.run(_tag_cases(cases))
    report = _report(cases, preds)

    ius = report["is_us"]
    if ius:
        logger.info(
            "is_us: acc=%.1f%% recall=%.1f%% precision=%.1f%% FNR=%.1f%% (n=%d) %s",
            ius["accuracy"] * 100,
            ius["recall"] * 100,
            ius["precision"] * 100,
            ius["false_negative_rate"] * 100,
            ius["scored"],
            ius["confusion"],
        )
    for m in report["is_us_misses"]:
        logger.info(
            "  is_us MISS: %r @ %r → pred=%s (conf %s), truth=%s",
            m["title"],
            m["location"],
            m["predicted_is_us"],
            m["us_confidence"],
            m["expected_is_us"],
        )
    rf = report["role_family"]
    if rf:
        logger.info("role_family: acc=%.1f%% (n=%d)", rf["accuracy"] * 100, rf["scored"])
        for m in rf["misses"]:
            logger.info(
                "  role_family MISS: expected=%s predicted=%s", m["expected"], m["predicted"]
            )
    if report["skipped_untagged"]:
        logger.info("skipped (tagger returned None): %d", report["skipped_untagged"])


if __name__ == "__main__":
    main()
