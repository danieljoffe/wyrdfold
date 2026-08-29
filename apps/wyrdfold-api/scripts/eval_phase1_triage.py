"""Eval 1: DeepSeek V3.2 vs Haiku 4.5 on Phase 1 title triage.

Plan reference: ``.claude/docs/plan-wyrdfold-multi-model-eval-coverage.md``
section "Eval 1 — Phase 1 title triage".

Question: can DeepSeek V3.2 (~10× cheaper than Haiku 4.5) match Haiku's
binary PROMISING/UNPROMISING decision well enough to be the production
Phase 1 model?

Approach
--------
- Pull titles from the existing eval_set.json fixture (89 (target, title)
  pairs spread across 3 active targets — plenty for a binary decision
  sanity check; an additional prod-DB-backed expansion to 200 lives in
  audit_phase1_fn.py and is out of scope here).
- Send each (target, title) batch through THREE models via OpenRouter:
    - haiku-4.5  (current production baseline — also the reference label)
    - sonnet-4.6 (quality ceiling, sanity check)
    - deepseek-v3.2 (candidate replacement)
- Each model gets the LITERAL Phase 1 prompt from
  ``app/services/relevance/title_triage.py::_SYSTEM_PROMPT`` and the
  same per-target user message format.
- Compute binary agreement + confusion matrix vs Haiku as the reference.

Acceptance thresholds (from the plan)
- DeepSeek ≥ 95% agreement with Haiku.
- DeepSeek false-positive rate ≤ 7% (DeepSeek says promising where Haiku
  said not). False-positives are cheap because Phase 2 catches noise;
  false-negatives are unrecoverable.

Cost expectation: ~$0.05 — DeepSeek is ~$0.30/1M output, titles are
short, Haiku is comparable, Sonnet bumps the bill by maybe $0.02.

Usage::

    cd apps/wyrdfold-api
    zsh -c 'source ~/.zshrc && uv run python scripts/eval_phase1_triage.py'
    zsh -c 'source ~/.zshrc && uv run python scripts/eval_phase1_triage.py --batch-size 25'

    # Global-triage bake-off against the unadmitted stack (see
    # scripts/build_phase1_unadmitted_corpus.py), oracle = sonnet:
    uv run python scripts/eval_phase1_triage.py \
        --fixture tests/fixtures/phase1_unadmitted_corpus.json \
        --reference sonnet-4.6 --batch-size 150 --temperature 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

# Make scripts._openrouter importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.targets import JobTarget
from app.services.relevance.title_triage import (
    _SYSTEM_PROMPT,
    TitleVerdict,
    _build_user_message,
    admitted,
)
from scripts._openrouter import MODELS, call_model, get_api_key

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("eval_phase1_triage")

_FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "eval_set.json"
_RESULTS_DIR = Path(__file__).parent / "eval_results"

# Candidate slugs — select a subset via --models. sonnet-4.6 is the usual
# oracle (--reference sonnet-4.6); the rest are triage-swap candidates weighed
# on agreement-with-oracle, FNR (dropping real matches is unrecoverable),
# cost, and latency.
_DEFAULT_MODELS: dict[str, str] = {
    "haiku-4.5": "anthropic/claude-haiku-4.5",
    "sonnet-4.6": MODELS["sonnet-4.6"],
    "deepseek-v3.2": MODELS["deepseek-v3.2"],
    "gemini-2.5-flash": "google/gemini-2.5-flash",
    "gemini-flash-lite": "google/gemini-2.5-flash-lite",
    "llama-3.3-70b": "meta-llama/llama-3.3-70b-instruct",
    "qwen3-235b": "qwen/qwen3-235b-a22b-2507",
    "mistral-small-3.2": "mistralai/mistral-small-3.2-24b-instruct",
}

# Phase 1 emits one TitleVerdict per id. A 25-title batch is ~750 tokens, but a
# prod-size 250-title batch is ~7.5K — size the cap to match prod so large-batch
# runs don't silently truncate (a 2K cap dropped verdicts → 0% coverage, an
# eval-only artifact; prod's triage_titles already uses 10240).
_MAX_OUTPUT_TOKENS = 10240

# Below this fraction of the reference's verdicts answered, a candidate model's
# agreement number is measured over too small a subset to trust — flagged as a
# hard fail regardless of how high that agreement is (#47).
_MIN_COVERAGE = 0.95

# Conservative concurrency: one call per (model, target) batch in
# parallel is plenty. OpenRouter rate limits are generous but DeepSeek
# can be slow under load.
_DEFAULT_CONCURRENCY = 6

# Default size of the un-triaged backlog a cost projection is quoted against:
# ~3 free-gate survivors per source-poll × ~4,800 enabled sources. Overridable
# with --projection-postings; the per-target multiplier defaults to however many
# targets the fixture carries, because Phase 1 grades EVERY survivor against
# EVERY unblocked active target (poller._poll_one_source) — that cross product is
# what makes catalog-wide triage a global cost rather than a per-user one.
_DEFAULT_PROJECTION_POSTINGS = 14_800


def _load_fixture(path: Path | None = None) -> dict[str, Any]:
    fixture_path = path or _FIXTURE_PATH
    if not fixture_path.exists():
        raise RuntimeError(
            f"Eval fixture missing: {fixture_path}\n"
            f"Run scripts/eval_grading_prompts.py --snapshot (drift/quality fixture) or "
            f"scripts/build_phase1_unadmitted_corpus.py (unadmitted-stack corpus) first."
        )
    return cast(dict[str, Any], json.loads(fixture_path.read_text()))


# ``JobTarget`` requires more fields than the Phase-1 prompt reads, and the
# published corpus deliberately stores only the fields it reads (see
# ``build_phase1_unadmitted_corpus._target_payload``). These neutral stubs make a
# minimized target constructible, and they are safe precisely BECAUSE the prompt
# never reads them: ``title_triage._split_user_message`` uses ``label`` and the
# two example pools, nothing else. A fixture carrying a real value overrides the
# stub, so the full-fidelity snapshot fixture is unaffected.
_TARGET_STUBS: dict[str, Any] = {
    "scoring_profile": {},
    "app_active": False,
    "created_at": "1970-01-01T00:00:00+00:00",
    "updated_at": "1970-01-01T00:00:00+00:00",
}


def _rehydrate_targets(fixture: dict[str, Any]) -> dict[str, JobTarget]:
    out: dict[str, JobTarget] = {}
    for tid, meta in fixture["targets"].items():
        out[tid] = JobTarget.model_validate({**_TARGET_STUBS, **meta["target"]})
    return out


def _titles_by_target(fixture: dict[str, Any]) -> dict[str, list[str]]:
    """Group fixture cases by target_id, preserving original order so the
    batch indices are stable across model runs."""
    groups: dict[str, list[str]] = defaultdict(list)
    for case in fixture["cases"]:
        tid = case["target_id"]
        title = case.get("title") or ""
        if title:
            groups[tid].append(title)
    return dict(groups)


def _labels_by_target(fixture: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """Ground-truth ``{target_id: {title: expected_promising}}`` from the
    fixture cases. Empty when the fixture carries no labels (e.g. a real-data
    snapshot) — the correctness report is then simply skipped, so drift-only
    fixtures keep working. See #193."""
    labels: dict[str, dict[str, bool]] = defaultdict(dict)
    for case in fixture["cases"]:
        tid = case["target_id"]
        title = case.get("title") or ""
        expected = case.get("expected_promising")
        if title and isinstance(expected, bool):
            labels[tid][title] = expected
    return {tid: m for tid, m in labels.items() if m}


def _strata_by_target(fixture: dict[str, Any]) -> dict[str, dict[str, str]]:
    """``{target_id: {title: stratum}}`` when the fixture tags its cases.

    The unadmitted-stack corpus tags every (target, title) pair ``own_gate`` or
    ``cross_gate`` — see ``build_phase1_unadmitted_corpus``. Both strata are real
    Phase-1 traffic and both are billed, but they are wildly different problems:
    ``cross_gate`` pairs are mostly obvious off-family rejects that any model
    gets right, so a headline agreement dominated by them can hide a model that
    is bad at the only pairs where the gate has to think. Empty for fixtures
    without tags — the stratum report is then simply skipped.
    """
    strata: dict[str, dict[str, str]] = defaultdict(dict)
    for case in fixture["cases"]:
        tid = case["target_id"]
        title = case.get("title") or ""
        stratum = case.get("stratum")
        if title and isinstance(stratum, str) and stratum:
            strata[tid][title] = stratum
    return {tid: m for tid, m in strata.items() if m}


def _chunk(seq: list[str], size: int) -> list[list[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _parse_verdicts(raw: dict[str, Any] | None) -> dict[int, bool]:
    """Pull the {id: promising} map out of a model response.

    Defensive: some models wrap verdicts under different keys, some skip
    ids, some emit non-int ids. Return only the well-formed entries so
    downstream agreement math is honest about what each model actually
    answered.
    """
    if not raw or not isinstance(raw, dict):
        return {}
    verdicts = raw.get("verdicts")
    if not isinstance(verdicts, list):
        # Some models flatten — try the top-level shape.
        return {}
    out: dict[int, bool] = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        try:
            vid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        prom = v.get("promising")
        if isinstance(prom, bool):
            out[vid] = prom
    return out


def _parse_confidences(raw: dict[str, Any] | None) -> dict[int, int]:
    """The 0-100 ``confidence`` alongside each verdict, where the model emitted
    a usable one.

    Kept separate from ``_parse_verdicts`` so the default report still measures
    the raw PROMISING call. It matters because production does NOT admit on
    ``promising`` alone: ``title_triage.admitted`` requires ``promising AND
    confidence >= settings.phase1_min_confidence`` (40 in prod), so a model that
    hedges its promising calls low is dropping postings a raw-verdict eval would
    score as catches. See ``--min-confidence``.
    """
    if not raw or not isinstance(raw, dict):
        return {}
    verdicts = raw.get("verdicts")
    if not isinstance(verdicts, list):
        return {}
    out: dict[int, int] = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        try:
            vid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        conf = v.get("confidence")
        # bool is an int subclass — a `"confidence": true` would sail through.
        if isinstance(conf, int) and not isinstance(conf, bool) and 0 <= conf <= 100:
            out[vid] = conf
    return out


def _apply_admission(results: list[dict[str, Any]], min_confidence: int) -> list[dict[str, Any]]:
    """Re-express each run's verdicts as production's ADMISSION decision.

    Uses the real ``title_triage.admitted`` rather than restating its rule, so
    an eval can never drift from the gate it is meant to be measuring. Verdicts
    the model never emitted stay absent — they fail open in prod, and coverage
    already accounts for them.
    """
    out: list[dict[str, Any]] = []
    for r in results:
        confidences: dict[int, int] = r.get("confidences") or {}
        gated: dict[int, bool] = {}
        for vid, prom in (r.get("verdicts") or {}).items():
            verdict = TitleVerdict(id=vid, promising=prom, confidence=confidences.get(vid))
            gated[vid] = admitted(verdict, min_confidence=min_confidence)
        out.append({**r, "verdicts": gated})
    return out


async def _grade_one_batch(
    *,
    target: JobTarget,
    titles: list[str],
    model_short: str,
    model_slug: str,
    api_key: str,
    temperature: float | None = None,
) -> dict[str, Any]:
    user_message = _build_user_message(target, titles)
    result = await call_model(
        model_slug=model_slug,
        system=_SYSTEM_PROMPT,
        user=user_message,
        api_key=api_key,
        max_tokens=_MAX_OUTPUT_TOKENS,
        temperature=temperature,
    )
    verdicts = _parse_verdicts(result.parsed)
    return {
        "model": model_short,
        "model_slug": model_slug,
        "target_id": target.id,
        "n_titles": len(titles),
        "n_verdicts": len(verdicts),
        "verdicts": verdicts,  # int -> bool, 1-based ids
        # Captured on every run (it is free) so the production admission rule
        # can be replayed offline without re-spending — see _apply_admission.
        "confidences": _parse_confidences(result.parsed),
        "raw_content_preview": result.raw_content[:200],
        "latency_ms": result.latency_ms,
        "cost_usd": result.cost_usd,
        "usage": result.usage,
        "error": result.error,
    }


async def _run_evaluation(
    *,
    targets: dict[str, JobTarget],
    titles_by_target: dict[str, list[str]],
    models: dict[str, str],
    batch_size: int,
    concurrency: int,
    api_key: str,
    inflight_path: Path,
    temperature: float | None = None,
) -> dict[str, Any]:
    # Plan: one job = (model, target, batch_chunk_index). Fire all jobs
    # with bounded concurrency; one call failing doesn't take down the
    # rest. We persist after each completed job so a network drop loses
    # at most one call's worth of data.
    sem = asyncio.Semaphore(concurrency)
    jobs: list[tuple[str, str, JobTarget, int, list[str]]] = []
    for tid, titles in titles_by_target.items():
        target = targets[tid]
        for chunk_idx, chunk in enumerate(_chunk(titles, batch_size)):
            for short, slug in models.items():
                jobs.append((short, slug, target, chunk_idx, chunk))

    total = len(jobs)
    logger.info("Total scheduled calls: %d", total)

    results: list[dict[str, Any]] = []

    async def _bounded(job: tuple[str, str, JobTarget, int, list[str]]) -> dict[str, Any]:
        short, slug, target, chunk_idx, chunk = job
        async with sem:
            out = await _grade_one_batch(
                target=target,
                titles=chunk,
                model_short=short,
                model_slug=slug,
                api_key=api_key,
                temperature=temperature,
            )
            out["chunk_idx"] = chunk_idx
            return out

    pending = {asyncio.create_task(_bounded(j)) for j in jobs}
    completed = 0
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            results.append(t.result())
            completed += 1
            # Inflight snapshot — network can die any time.
            inflight_path.write_text(
                json.dumps(
                    {
                        "completed": completed,
                        "total": total,
                        "models": models,
                        "captured_at_unix": int(time.time()),
                        "results_so_far": results,
                    },
                    indent=2,
                )
            )
            if completed % max(1, total // 10) == 0 or completed == total:
                logger.info(
                    "Progress: %d/%d (%d%%)",
                    completed,
                    total,
                    100 * completed // total,
                )

    return {"results": results, "models": models}


def _score_pairs(
    by_key: dict[tuple[str, str, int], dict[int, bool]],
    chunk_keys: list[tuple[str, int]],
    model: str,
    reference: str,
    *,
    include: Callable[[str, int, int], bool] | None = None,
) -> dict[str, Any]:
    """Confusion matrix + coverage for one model against the reference.

    ``include(target_id, chunk_idx, verdict_id)`` restricts the comparison to a
    subset of the pairs (used for the per-stratum breakdown). ``None`` scores
    every pair — the behaviour this function was extracted from, unchanged.
    """
    tp = fp = tn = fn = total_compared = 0
    missing_in_model = 0
    # Of the un-answered titles, how many the reference marked PROMISING —
    # these are the "lost forever" misses a Phase-1 gate most fears (#47).
    missing_promising = 0
    missing_in_ref = 0
    for tid, ci in chunk_keys:
        ref_v = by_key.get((reference, tid, ci), {})
        mod_v = by_key.get((model, tid, ci), {})
        # Compare verdicts only on ids where BOTH model emitted a bool.
        for vid, ref_prom in ref_v.items():
            if include is not None and not include(tid, ci, vid):
                continue
            if vid not in mod_v:
                missing_in_model += 1
                if ref_prom:
                    missing_promising += 1
                continue
            mod_prom = mod_v[vid]
            total_compared += 1
            if ref_prom and mod_prom:
                tp += 1
            elif ref_prom and not mod_prom:
                fn += 1
            elif (not ref_prom) and mod_prom:
                fp += 1
            else:
                tn += 1
        for vid in mod_v:
            if include is not None and not include(tid, ci, vid):
                continue
            if vid not in ref_v:
                missing_in_ref += 1
    n = max(1, total_compared)
    graded_by_ref = total_compared + missing_in_model
    return {
        "compared": total_compared,
        "agreement_rate": round((tp + tn) / n, 4),
        "false_positive_rate": round(fp / max(1, fp + tn), 4),
        "false_negative_rate": round(fn / max(1, fn + tp), 4),
        # Fraction of the reference's verdicts this model actually answered.
        # Agreement is computed only over answered pairs, so a model that
        # drops titles can look accurate on the few it graded — coverage is
        # the honesty check, and low coverage is a hard fail regardless of
        # agreement (#47).
        "coverage": round(total_compared / max(1, graded_by_ref), 4),
        # FNR that treats an un-answered PROMISING title as a miss (fail-open
        # saves it in prod, but for the eval a gate that can't emit a verdict
        # is unreliable exactly where it's most dangerous).
        "false_negative_rate_with_coverage": round(
            (fn + missing_promising) / max(1, fn + tp + missing_promising), 4
        ),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "missing_verdicts_in_model": missing_in_model,
        "missing_promising_in_model": missing_promising,
        "missing_verdicts_in_ref": missing_in_ref,
    }


def _agreement_report(
    results: list[dict[str, Any]],
    titles_by_target: dict[str, list[str]],
    models: dict[str, str],
    *,
    reference: str = "haiku-4.5",
    strata_by_key: dict[tuple[str, int, int], str] | None = None,
    projection_postings: int = _DEFAULT_PROJECTION_POSTINGS,
    projection_targets: int | None = None,
) -> dict[str, Any]:
    """Compute per-model binary agreement with the reference model
    (Haiku), aggregated over every (target, title) the reference graded.

    Also returns confusion matrix entries so the FPR / FNR thresholds
    in the plan can be checked directly.
    """
    # Build per-(model, target_id, chunk_idx) verdict dict for easy joining.
    by_key: dict[tuple[str, str, int], dict[int, bool]] = {}
    by_model_cost: dict[str, float] = defaultdict(float)
    by_model_latency: dict[str, list[int]] = defaultdict(list)
    by_model_errors: dict[str, int] = defaultdict(int)
    # Titles SENT, not verdicts returned — you pay for the prompt whether or not
    # the model answers, so this is the honest denominator for a $/1k figure.
    by_model_titles: dict[str, int] = defaultdict(int)
    for r in results:
        key = (r["model"], r["target_id"], r["chunk_idx"])
        by_key[key] = r["verdicts"] or {}
        by_model_cost[r["model"]] += r.get("cost_usd", 0.0)
        by_model_latency[r["model"]].append(r.get("latency_ms", 0))
        by_model_titles[r["model"]] += int(r.get("n_titles", 0))
        if r.get("error"):
            by_model_errors[r["model"]] += 1

    chunk_keys = sorted({(tid, ci) for (_, tid, ci) in by_key})
    n_targets = projection_targets if projection_targets is not None else len(titles_by_target)
    projected_pairs = projection_postings * max(1, n_targets)

    def _spend(model: str) -> dict[str, Any]:
        lat = sorted(by_model_latency[model])
        sent = by_model_titles[model]
        per_1k = (by_model_cost[model] / sent * 1000) if sent else 0.0
        return {
            "titles_sent": sent,
            "total_cost_usd": round(by_model_cost[model], 5),
            "cost_per_1k_titles_usd": round(per_1k, 5),
            "projected_backlog_usd": round(per_1k * projected_pairs / 1000, 4),
            "avg_latency_ms": int(sum(lat) / max(1, len(lat))),
            "p95_latency_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else 0,
            "max_latency_ms": lat[-1] if lat else 0,
            "errored_batches": by_model_errors[model],
        }

    per_model: dict[str, dict[str, Any]] = {}
    for model in models:
        if model == reference:
            continue
        per_model[model] = {
            **_score_pairs(by_key, chunk_keys, model, reference),
            **_spend(model),
        }
        if strata_by_key:
            per_model[model]["per_stratum"] = {
                stratum: _score_pairs(
                    by_key,
                    chunk_keys,
                    model,
                    reference,
                    include=lambda t, c, v, _s=stratum: strata_by_key.get((t, c, v)) == _s,
                )
                for stratum in sorted(set(strata_by_key.values()))
            }

    # Also surface reference cost so the writeup can quote it.
    per_model[reference] = _spend(reference)

    # Per-target breakdown so disagreements can be traced to a target.
    per_target_agreement: dict[str, dict[str, float]] = {}
    for tid in titles_by_target:
        per_target_agreement[tid] = {}
        for model in models:
            if model == reference:
                continue
            agree = total = 0
            for ci in {ci for (_, t, ci) in by_key if t == tid}:
                ref_v = by_key.get((reference, tid, ci), {})
                mod_v = by_key.get((model, tid, ci), {})
                for vid, ref_prom in ref_v.items():
                    if vid in mod_v:
                        total += 1
                        if mod_v[vid] == ref_prom:
                            agree += 1
            per_target_agreement[tid][model] = round(agree / total, 4) if total else 0.0

    return {
        "reference_model": reference,
        "projection": {
            "postings": projection_postings,
            "targets": n_targets,
            "pairs": projected_pairs,
        },
        "per_model": per_model,
        "per_target_agreement": per_target_agreement,
    }


def _correctness_report(
    results: list[dict[str, Any]],
    titles_by_target: dict[str, list[str]],
    labels_by_target: dict[str, dict[str, bool]],
    *,
    batch_size: int,
) -> dict[str, dict[str, Any]]:
    """Per-model correctness vs the fixture's ground-truth labels (#193).

    Unlike ``_agreement_report`` (model-vs-reference *drift*), this scores each
    model's PROMISING verdicts against the labeled expectation: recall (of the
    truly-promising titles, how many the model caught), precision, false-
    negative rate, and accuracy. Returns ``{}`` when the fixture carries no
    labels (a real-data snapshot), so drift-only fixtures are unaffected.

    Verdict ids are 1-based positions within each (target, chunk); we rebuild
    the same chunking to map ``(target, chunk, id) -> title -> label``.
    """
    if not labels_by_target:
        return {}

    label_by_key: dict[tuple[str, int, int], bool] = {}
    for tid, titles in titles_by_target.items():
        target_labels = labels_by_target.get(tid, {})
        for ci, chunk in enumerate(_chunk(titles, batch_size)):
            for pos, title in enumerate(chunk, start=1):
                if title in target_labels:
                    label_by_key[(tid, ci, pos)] = target_labels[title]

    tally: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "scored": 0, "unlabeled": 0}
    )
    for r in results:
        acc = tally[r["model"]]
        for vid, pred in (r["verdicts"] or {}).items():
            label = label_by_key.get((r["target_id"], r["chunk_idx"], vid))
            if label is None:
                acc["unlabeled"] += 1
                continue
            acc["scored"] += 1
            if label and pred:
                acc["tp"] += 1
            elif label and not pred:
                acc["fn"] += 1
            elif (not label) and pred:
                acc["fp"] += 1
            else:
                acc["tn"] += 1

    out: dict[str, dict[str, Any]] = {}
    for model, c in tally.items():
        tp, fp, tn, fn = c["tp"], c["fp"], c["tn"], c["fn"]
        out[model] = {
            "scored": c["scored"],
            "recall": round(tp / max(1, tp + fn), 4),
            "precision": round(tp / max(1, tp + fp), 4),
            "false_negative_rate": round(fn / max(1, tp + fn), 4),
            "accuracy": round((tp + tn) / max(1, tp + fp + tn + fn), 4),
            "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
            "unlabeled_verdicts": c["unlabeled"],
        }
    return out


def _strata_by_key(
    titles_by_target: dict[str, list[str]],
    strata_by_target: dict[str, dict[str, str]],
    *,
    batch_size: int,
) -> dict[tuple[str, int, int], str]:
    """``(target_id, chunk_idx, 1-based id) -> stratum``.

    Verdict ids are positions within a (target, chunk), so the same chunking the
    run used has to be rebuilt to map an id back to its title. Mirrors the
    ``label_by_key`` construction in ``_correctness_report``.
    """
    out: dict[tuple[str, int, int], str] = {}
    for tid, titles in titles_by_target.items():
        target_strata = strata_by_target.get(tid, {})
        for ci, chunk in enumerate(_chunk(titles, batch_size)):
            for pos, title in enumerate(chunk, start=1):
                stratum = target_strata.get(title)
                if stratum:
                    out[(tid, ci, pos)] = stratum
    return out


def _write_report(
    *,
    final: dict[str, Any],
    report: dict[str, Any],
    titles_by_target: dict[str, list[str]],
    targets: dict[str, JobTarget],
    output_base: Path,
) -> None:
    output_base.parent.mkdir(parents=True, exist_ok=True)
    raw_path = output_base.with_suffix(".json")
    md_path = output_base.with_suffix(".md")

    raw_path.write_text(
        json.dumps(
            {
                "captured_at_unix": int(time.time()),
                "models": final["models"],
                "report": report,
                "results": final["results"],
            },
            indent=2,
        )
    )

    # Build the markdown writeup.
    ref = report["reference_model"]
    md: list[str] = []
    md.append("# Phase 1 Title Triage — Multi-Model Run")
    md.append("")
    md.append(f"- Reference model: **{ref}** (production baseline)")
    n_titles = sum(len(v) for v in titles_by_target.values())
    md.append(f"- Titles graded: **{n_titles}** across {len(titles_by_target)} targets")
    md.append("")

    proj = report.get("projection") or {}
    md.append("## Per-model summary")
    md.append("")
    md.append(
        "FN is called out first on purpose: a false negative is a posting Phase 1 "
        "drops, and a dropped posting is never ingested and never re-triaged, so the "
        "loss is unrecoverable. A false positive only costs one Phase-2 grade."
    )
    md.append("")
    md.append(
        "| Model | **FNR** | FNR+cov | FPR | Agreement vs ref | Coverage | "
        "Compared | $ total | $/1k titles | Proj. backlog | Avg latency | p95 | Errors |"
    )
    md.append("| --- |" + " --- |" * 12)
    for model, stats in report["per_model"].items():
        if model == ref:
            md.append(
                f"| {model} (ref) | — | — | — | — | — | — | "
                f"${stats['total_cost_usd']:.4f} | "
                f"${stats['cost_per_1k_titles_usd']:.4f} | "
                f"${stats['projected_backlog_usd']:.2f} | "
                f"{stats['avg_latency_ms']}ms | {stats['p95_latency_ms']}ms | "
                f"{stats['errored_batches']} |"
            )
        else:
            cov = stats["coverage"]
            # Flag low coverage: agreement over a small answered subset is not
            # trustworthy (#47).
            cov_cell = f"{cov * 100:.1f}%" + (" ⚠️" if cov < _MIN_COVERAGE else "")
            md.append(
                f"| {model} | **{stats['false_negative_rate'] * 100:.1f}%** | "
                f"{stats['false_negative_rate_with_coverage'] * 100:.1f}% | "
                f"{stats['false_positive_rate'] * 100:.1f}% | "
                f"{stats['agreement_rate'] * 100:.1f}% | "
                f"{cov_cell} | "
                f"{stats['compared']} | ${stats['total_cost_usd']:.4f} | "
                f"${stats['cost_per_1k_titles_usd']:.4f} | "
                f"${stats['projected_backlog_usd']:.2f} | "
                f"{stats['avg_latency_ms']}ms | {stats['p95_latency_ms']}ms | "
                f"{stats['errored_batches']} |"
            )
    if proj:
        md.append("")
        md.append(
            f"> Projected backlog = {proj['postings']:,} un-triaged postings × "
            f"{proj['targets']} active targets = {proj['pairs']:,} (target, title) pairs. "
            "Phase 1 grades every free-gate survivor against every unblocked target, so "
            "the target count is a multiplier on the bill."
        )
    low_cov = [
        m
        for m, s in report["per_model"].items()
        if m != ref and s.get("coverage", 1.0) < _MIN_COVERAGE
    ]
    if low_cov:
        md.append("")
        md.append(
            f"> ⚠️ **Low coverage** (< {_MIN_COVERAGE:.0%}): {', '.join(low_cov)} "
            "dropped too many titles for the agreement numbers to be trusted — "
            "treat as a hard fail regardless of agreement."
        )
    md.append("")

    strata = sorted(
        {
            s
            for m, stats in report["per_model"].items()
            if m != ref
            for s in (stats.get("per_stratum") or {})
        }
    )
    if strata:
        md.append("## Per-stratum breakdown")
        md.append("")
        md.append(
            "`own_gate` = this target's own free gate admits the title (the hard, "
            "ambiguous pairs). `cross_gate` = the title only survived because a "
            "*different* target's gate admitted it (mostly easy off-family rejects). "
            "A headline agreement carried by `cross_gate` says little."
        )
        md.append("")
        md.append("| Model | " + " | ".join(f"{s} agree / FNR / cov (n)" for s in strata) + " |")
        md.append("| --- |" + " --- |" * len(strata))
        for model, stats in report["per_model"].items():
            if model == ref or not stats.get("per_stratum"):
                continue
            cells = []
            for s in strata:
                b = stats["per_stratum"].get(s) or {}
                cells.append(
                    f"{b.get('agreement_rate', 0) * 100:.1f}% / "
                    f"{b.get('false_negative_rate', 0) * 100:.1f}% / "
                    f"{b.get('coverage', 0) * 100:.1f}% ({b.get('compared', 0)})"
                )
            md.append(f"| {model} | " + " | ".join(cells) + " |")
        md.append("")
    gated = report.get("admission_gated")
    if gated:
        min_conf = (report.get("run") or {}).get("min_confidence")
        md.append(f"## Production admission decision (promising AND confidence >= {min_conf})")
        md.append("")
        md.append(
            "The table above scores the raw PROMISING verdict. Production admits on "
            "`title_triage.admitted` — a promising call the model hedges below the "
            "confidence floor is DROPPED. Same responses, re-scored under that rule."
        )
        md.append("")
        md.append("| Model | **FNR** | FPR | Agreement vs ref | Coverage | Compared |")
        md.append("| --- | --- | --- | --- | --- | --- |")
        for model, stats in gated["per_model"].items():
            if model == ref or "agreement_rate" not in stats:
                continue
            md.append(
                f"| {model} | **{stats['false_negative_rate'] * 100:.1f}%** | "
                f"{stats['false_positive_rate'] * 100:.1f}% | "
                f"{stats['agreement_rate'] * 100:.1f}% | "
                f"{stats['coverage'] * 100:.1f}% | {stats['compared']} |"
            )
        md.append("")

    md.append("## Per-target agreement")
    md.append("")
    md.append("| Target | " + " | ".join(m for m in final["models"] if m != ref) + " |")
    md.append("| --- |" + " --- |" * (len(final["models"]) - 1))
    for tid, by_model in report["per_target_agreement"].items():
        label = targets[tid].label if tid in targets else tid
        cells = []
        for m in final["models"]:
            if m == ref:
                continue
            cells.append(f"{by_model.get(m, 0.0) * 100:.1f}%")
        md.append(f"| {label[:40]} | " + " | ".join(cells) + " |")
    md.append("")

    md_path.write_text("\n".join(md))
    logger.info("Wrote %s", raw_path)
    logger.info("Wrote %s", md_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="Titles per LLM call (default 25; prod default is 250).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=_DEFAULT_CONCURRENCY,
        help="Parallel OpenRouter calls.",
    )
    parser.add_argument(
        "--models",
        type=str,
        default=None,
        help="Comma-separated subset of {haiku-4.5,sonnet-4.6,deepseek-v3.2}.",
    )
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument(
        "--reference",
        type=str,
        default="haiku-4.5",
        help="Reference/oracle model to score agreement against (default haiku-4.5). "
        "Use e.g. sonnet-4.6 to treat the strongest model as ground truth.",
    )
    parser.add_argument(
        "--fixture",
        type=str,
        default=None,
        help="Fixture to grade (default tests/fixtures/eval_set.json). Point at "
        "tests/fixtures/phase1_unadmitted_corpus.json for the unadmitted-stack corpus.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature. Left UNSET by default (provider picks, ~1.0) so "
        "existing runs are comparable — but production's complete_json sends 0.0, so "
        "pass --temperature 0 for a decision-grade bake-off.",
    )
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=None,
        help="Also report agreement on production's ADMISSION decision "
        "(promising AND confidence >= N, per title_triage.admitted) instead of the "
        "raw PROMISING verdict alone. Prod runs 40 (PHASE1_MIN_CONFIDENCE). Costs "
        "nothing extra — it replays the same responses.",
    )
    parser.add_argument(
        "--projection-postings",
        type=int,
        default=_DEFAULT_PROJECTION_POSTINGS,
        help=f"Un-triaged postings to project a backlog cost over (default "
        f"{_DEFAULT_PROJECTION_POSTINGS:,}). Multiplied by the fixture's target count, "
        f"because every survivor is graded against every active target.",
    )
    args = parser.parse_args()

    fixture = _load_fixture(Path(args.fixture) if args.fixture else None)
    targets = _rehydrate_targets(fixture)
    titles_by_target = _titles_by_target(fixture)

    if args.models:
        wanted = {m.strip() for m in args.models.split(",") if m.strip()}
        models = {k: v for k, v in _DEFAULT_MODELS.items() if k in wanted}
        if not models:
            raise SystemExit(f"No matching models in --models={args.models!r}.")
    else:
        models = _DEFAULT_MODELS

    # Fail fast BEFORE spending on the eval run if the oracle isn't in the set.
    if args.reference not in models:
        raise SystemExit(
            f"--reference={args.reference!r} must be one of the run's models: {sorted(models)}"
        )

    n_titles = sum(len(v) for v in titles_by_target.values())
    n_batches = sum(len(_chunk(v, args.batch_size)) for v in titles_by_target.values())
    logger.info(
        "Fixture: %d titles across %d targets → %d batches × %d models = %d calls",
        n_titles,
        len(titles_by_target),
        n_batches,
        len(models),
        n_batches * len(models),
    )
    logger.info("Models: %s", ", ".join(f"{k}={v}" for k, v in models.items()))

    api_key = get_api_key()

    ts = time.strftime("%Y%m%dT%H%M%S")
    base = Path(args.output) if args.output else (_RESULTS_DIR / f"eval_phase1_triage_{ts}")
    base.parent.mkdir(parents=True, exist_ok=True)
    inflight = base.with_suffix(".inflight.json")

    final = asyncio.run(
        _run_evaluation(
            targets=targets,
            titles_by_target=titles_by_target,
            models=models,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            api_key=api_key,
            inflight_path=inflight,
            temperature=args.temperature,
        )
    )

    report = _agreement_report(
        final["results"],
        titles_by_target,
        models,
        reference=args.reference,
        strata_by_key=_strata_by_key(
            titles_by_target, _strata_by_target(fixture), batch_size=args.batch_size
        )
        or None,
        projection_postings=args.projection_postings,
    )
    report["run"] = {
        "fixture": str(Path(args.fixture) if args.fixture else _FIXTURE_PATH),
        "batch_size": args.batch_size,
        "temperature": args.temperature,
        "min_confidence": args.min_confidence,
        "fixture_meta": fixture.get("meta"),
    }
    if args.min_confidence is not None:
        # Same responses, re-scored under production's admission rule. The two
        # numbers answer different questions: the raw one is "does this model
        # read titles like the oracle", the gated one is "would this model admit
        # the same postings prod would".
        report["admission_gated"] = _agreement_report(
            _apply_admission(final["results"], args.min_confidence),
            titles_by_target,
            models,
            reference=args.reference,
            projection_postings=args.projection_postings,
        )
    # Correctness vs ground-truth labels when the fixture carries them (#193):
    # measures each model against TRUTH, not just cross-model agreement. Skipped
    # (empty) for unlabeled real-data snapshots.
    correctness = _correctness_report(
        final["results"],
        titles_by_target,
        _labels_by_target(fixture),
        batch_size=args.batch_size,
    )
    if correctness:
        report["per_model_correctness"] = correctness
    _write_report(
        final=final,
        report=report,
        titles_by_target=titles_by_target,
        targets=targets,
        output_base=base,
    )

    # Surface the headline numbers on stdout, FN first — a false negative is a
    # posting dropped forever, a false positive is one wasted Phase-2 grade.
    logger.info("")
    logger.info("vs %s (FN first — FN is the unrecoverable error):", args.reference)
    ranked = sorted(
        ((m, s) for m, s in report["per_model"].items() if m != args.reference),
        key=lambda kv: (kv[1].get("false_negative_rate", 1.0), -kv[1].get("coverage", 0.0)),
    )
    for model, s in ranked:
        flag = " ⚠️LOW-COVERAGE" if s.get("coverage", 1.0) < _MIN_COVERAGE else ""
        logger.info(
            "  %-20s FNR=%5.1f%%  FPR=%5.1f%%  agree=%5.1f%%  cov=%5.1f%%  "
            "$/1k=%.4f  proj=$%.2f  lat=%dms%s",
            model,
            s.get("false_negative_rate", 0) * 100,
            s.get("false_positive_rate", 0) * 100,
            s.get("agreement_rate", 0) * 100,
            s.get("coverage", 0) * 100,
            s.get("cost_per_1k_titles_usd", 0.0),
            s.get("projected_backlog_usd", 0.0),
            s.get("avg_latency_ms", 0),
            flag,
        )
    if report.get("admission_gated"):
        logger.info("")
        logger.info(
            "Under production's admission rule (promising AND confidence >= %d):",
            args.min_confidence,
        )
        for model, s in report["admission_gated"]["per_model"].items():
            if model == args.reference or "agreement_rate" not in s:
                continue
            logger.info(
                "  %-20s FNR=%5.1f%%  FPR=%5.1f%%  agree=%5.1f%%",
                model,
                s["false_negative_rate"] * 100,
                s["false_positive_rate"] * 100,
                s["agreement_rate"] * 100,
            )
    for model, c in correctness.items():
        logger.info(
            "%s vs labels: recall=%.1f%% precision=%.1f%% accuracy=%.1f%% (n=%d)",
            model,
            c["recall"] * 100,
            c["precision"] * 100,
            c["accuracy"] * 100,
            c["scored"],
        )


if __name__ == "__main__":
    main()
