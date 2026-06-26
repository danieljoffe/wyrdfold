"""One-time clean-label bootstrap (#60, Phase 2). COSTS LLM $ — read first.

The pre-scan threshold (``calibrate_prescan_threshold.py``) must be calibrated
on CLEAN labels — LLM fit grades — NOT the keyword ``scores.score`` (which #60
proved is polluted; the frontend corpus has only a handful of ``complete`` LLM
rows). This script produces those clean labels: per active target it samples a
BALANCED set of that target's candidate jobs, STRATIFIED across the keyword-score
range, and runs the EXISTING Phase-2 LLM fit grader (``derive_job_fit``) on each
to get a clean 0-100 fit score. It writes ``(job_id, target_id, clean_score)``
rows to ``--out`` as JSON.

Why stratified: a threshold learned only on already-promising jobs never sees
the FALSE-POSITIVE band — off-domain postings that nonetheless score keyword
points (a "Sales Engineer" for an engineering target, a JD that name-drops the
right tech in passing). Those are exactly the jobs the cosine gate must learn to
REJECT, so the sample deliberately spans the whole keyword-score range, not just
the top.

This grades against the target's owning user's optimized profile (same contract
as ``backfill_phase2_fit.py``) — Phase 2 is per-(user, target, job). It does NOT
write to the ``scores`` table or anywhere in prod; the ONLY output is the JSON
file. It is resumable: an existing ``--out`` file is loaded and already-labelled
(job, target) pairs are skipped, so re-running after an interruption only grades
what's missing.

COST: ~$0.0035 per job (Sonnet). 200 jobs × N targets ≈ $0.70 × N. DO NOT run it
casually — it spends real LLM budget. ``--dry-run`` reports the sampled counts
per target and spends nothing.

Usage:
    cd apps/wyrdfold-api && uv run python scripts/bootstrap_clean_labels.py --dry-run
    cd apps/wyrdfold-api && railway run uv run python scripts/bootstrap_clean_labels.py \
        --out tests/fixtures/prescan_clean_labels.json --per-target 250
    # resume after Ctrl-C: same command — already-graded pairs are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, cast

from app.services.experience.optimized import get_latest as get_latest_optimized
from app.services.fit.job_fit import derive_job_fit
from app.services.llm import cost_log
from app.services.llm import get_default_client as get_llm_client
from app.services.scoring import strip_html
from app.services.targets.crud import get_active as get_active_targets
from app.supabase_pool import get_supabase_pool, init_supabase

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("bootstrap_clean_labels")

# Cost-log label so the bootstrap spend is sliceable apart from live Phase-2 grading.
BOOTSTRAP_PURPOSE = "prescan.bootstrap_label"

_PAGE = 1000
# Number of stratification buckets across the keyword-score range. The sample is
# spread evenly across these so the low / mid / high keyword bands are all
# represented (the mid/low bands carry the false-positive examples).
_STRATA = 5

# Fraction of each target's sample drawn from the TOP keyword-score quartile
# (where LLM-graded genuine fits concentrate). #60 found the plain even-band
# sample positive-starved — ~8 fits/target, too few to fix a recall threshold —
# because the corpus is bottom-heavy, so equal-WIDTH bands leave the top band
# sparse. Pulling half the sample from the top QUANTILE surfaces many more
# positives while the even-band remainder still covers the low/mid
# false-positive band. Tunable via --top-band-fraction; 0 = pure even-band.
_DEFAULT_TOP_BAND_FRACTION = 0.5


def _fetch_scored_jobs(supabase: Any, target_id: str) -> list[dict[str, Any]]:
    """All (job_posting_id, score) rows for a target, paginated.

    ``score`` is the keyword pipeline's integer score — the stratification axis,
    NOT a clean label. A NULL score sorts as the lowest bucket.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = (
            supabase.table("scores")
            .select("job_posting_id, score")
            .eq("target_id", target_id)
            .range(offset, offset + _PAGE - 1)
            .execute()
        )
        page = cast(list[dict[str, Any]], resp.data or [])
        if not page:
            break
        rows.extend(page)
        if len(page) < _PAGE:
            break
        offset += _PAGE
    return rows


def _even_band_sample(rows: list[dict[str, Any]], *, n: int, rng: random.Random) -> list[str]:
    """Pick ~n job ids spread evenly across the keyword-score range.

    Buckets the rows into ``_STRATA`` equal-width score bands and draws an even
    quota from each (topping up from the global remainder when a band is thin),
    so the sample spans the whole keyword-score range — including the low/mid
    off-domain-but-keyword-plausible band (the false positives the threshold
    must learn to reject). Returns at most ``n`` ids.
    """
    if not rows or n <= 0:
        return []
    if len(rows) <= n:
        return [r["job_posting_id"] for r in rows]

    scores = [float(r.get("score") or 0) for r in rows]
    lo, hi = min(scores), max(scores)
    span = hi - lo

    # Assign each row to a band. Degenerate (all-equal) score range → one band,
    # which collapses to a plain random sample below.
    buckets: list[list[str]] = [[] for _ in range(_STRATA)]
    for r in rows:
        s = float(r.get("score") or 0)
        idx = 0 if span == 0 else min(_STRATA - 1, int((s - lo) / span * _STRATA))
        buckets[idx].append(r["job_posting_id"])

    for b in buckets:
        rng.shuffle(b)

    per_band = max(1, n // _STRATA)
    picked: list[str] = []
    leftovers: list[str] = []
    for b in buckets:
        picked.extend(b[:per_band])
        leftovers.extend(b[per_band:])

    # Top up to exactly n from the pooled remainder (keeps the total stable when
    # some bands were thinner than the per-band quota).
    if len(picked) < n and leftovers:
        rng.shuffle(leftovers)
        picked.extend(leftovers[: n - len(picked)])
    return picked[:n]


def _stratified_sample(
    rows: list[dict[str, Any]],
    *,
    n: int,
    rng: random.Random,
    top_band_fraction: float = _DEFAULT_TOP_BAND_FRACTION,
) -> list[str]:
    """Pick ~n job ids, biased toward high keyword-score jobs.

    ``top_band_fraction`` of ``n`` is drawn from the TOP keyword-score quartile,
    where the LLM-graded genuine fits concentrate. #60 found the plain even-band
    sample (:func:`_even_band_sample`) positive-starved: the corpus is
    bottom-heavy, so equal-WIDTH bands make the top band sparse and the
    calibration set ends up with only a handful of fits — too few to fix a recall
    threshold. Taking the top quartile by QUANTILE (always populated) surfaces
    many more positives. The remaining ``1 - top_band_fraction`` is drawn via the
    even-band sampler so the low/mid false-positive band the gate must reject
    stays represented. ``top_band_fraction <= 0`` reproduces the pure even-band
    sample. Returns at most ``n`` ids.
    """
    if not rows or n <= 0:
        return []
    if len(rows) <= n:
        return [r["job_posting_id"] for r in rows]

    frac = max(0.0, min(1.0, top_band_fraction))
    n_top = round(n * frac)

    picked: list[str] = []
    picked_set: set[str] = set()
    if n_top > 0:
        # Top quartile by keyword score (quantile → populated even on a
        # bottom-heavy score distribution, unlike an equal-width top band).
        by_score = sorted(rows, key=lambda r: float(r.get("score") or 0))
        top_quartile = by_score[int(len(by_score) * 0.75) :]
        top_ids = [r["job_posting_id"] for r in top_quartile]
        rng.shuffle(top_ids)
        picked = top_ids[:n_top]
        picked_set = set(picked)

    # Remainder: even-band over rows not already taken, so the low/mid bands
    # (the leakage the gate must reject) stay represented.
    n_rest = n - len(picked)
    if n_rest > 0:
        remaining = [r for r in rows if r["job_posting_id"] not in picked_set]
        picked.extend(_even_band_sample(remaining, n=n_rest, rng=rng))
    return picked[:n]


def _fetch_jobs(supabase: Any, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch (id, title, description_html) for the sampled jobs, keyed by id."""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(job_ids), _PAGE):
        chunk = job_ids[i : i + _PAGE]
        resp = (
            supabase.table("jobs").select("id, title, description_html").in_("id", chunk).execute()
        )
        for row in cast(list[dict[str, Any]], resp.data or []):
            out[row["id"]] = row
    return out


def _resolve_target_user(supabase: Any, target_id: str) -> tuple[str, Any] | None:
    """Return ``(user_id, optimized_payload)`` for an active target, or None.

    Mirrors ``backfill_phase2_fit._resolve_target_user``: Phase 2 grades a job
    against the owning user's optimized profile, so a target with no profiled
    active owner is skipped (nothing to grade against).
    """
    resp = (
        supabase.table("user_targets")
        .select("user_id")
        .eq("target_id", target_id)
        .eq("is_active", True)
        .execute()
    )
    for row in cast(list[dict[str, Any]], resp.data or []):
        doc = get_latest_optimized(supabase, row["user_id"])
        if doc is not None:
            return row["user_id"], doc.payload
    return None


def _load_existing(out_path: Path) -> list[dict[str, Any]]:
    """Load already-written labels (for resumability). Missing file → []."""
    if not out_path.exists():
        return []
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
        return cast(list[dict[str, Any]], data.get("labels", []))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not parse %s — starting fresh", out_path)
        return []


def _write_labels(out_path: Path, labels: list[dict[str, Any]]) -> None:
    """Persist labels atomically (write temp, then replace)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "prescan_clean_labels.v1",
        "purpose": BOOTSTRAP_PURPOSE,
        "labels": labels,  # [{job_id, target_id, clean_score}]
    }
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(out_path)


async def bootstrap(
    *,
    out_path: Path,
    per_target: int,
    concurrency: int,
    target_id: str | None,
    dry_run: bool,
    seed: int,
    top_band_fraction: float = _DEFAULT_TOP_BAND_FRACTION,
) -> int:
    init_supabase()
    supabase = get_supabase_pool()
    if supabase is None:
        raise SystemExit("Supabase not configured (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)")

    rng = random.Random(seed)

    targets = get_active_targets(supabase)
    if target_id:
        targets = [t for t in targets if t.id == target_id]
    logger.info("Bootstrapping clean labels for %d active target(s)", len(targets))

    labels = _load_existing(out_path)
    done_pairs = {(row["job_id"], row["target_id"]) for row in labels}
    if done_pairs:
        logger.info("Resuming — %d label(s) already on disk", len(done_pairs))

    llm = get_llm_client()
    sem = asyncio.Semaphore(concurrency)
    newly_written = 0

    for target in targets:
        all_rows = _fetch_scored_jobs(supabase, target.id)
        if not all_rows:
            logger.info("%s — no scored jobs, skipping", target.label)
            continue

        sampled_ids = _stratified_sample(
            all_rows, n=per_target, rng=rng, top_band_fraction=top_band_fraction
        )
        pending = [jid for jid in sampled_ids if (jid, target.id) not in done_pairs]

        if dry_run:
            logger.info(
                "%s — %d scored, sampled %d (%d already labelled, %d to grade) [dry-run]",
                target.label,
                len(all_rows),
                len(sampled_ids),
                len(sampled_ids) - len(pending),
                len(pending),
            )
            continue

        resolved = _resolve_target_user(supabase, target.id)
        if resolved is None:
            logger.warning(
                "Skipping target %s (%s) — no active owner with a profile",
                target.id,
                target.label,
            )
            continue
        user_id, payload = resolved

        if not pending:
            logger.info("%s — all %d sampled already labelled", target.label, len(sampled_ids))
            continue

        jobs = _fetch_jobs(supabase, pending)

        async def _grade(
            jid: str, *, _payload: Any = payload, _uid: str = user_id
        ) -> dict[str, Any] | None:
            job = jobs.get(jid)
            if job is None:
                return None
            async with sem:
                try:
                    fit, llm_result = await derive_job_fit(
                        llm,
                        payload=_payload,
                        target=target,
                        job_title=job.get("title", "") or "",
                        jd_text=strip_html(job.get("description_html", "") or ""),
                    )
                except Exception:
                    logger.exception("Grade failed for job %s / target %s", jid, target.id)
                    return None
            # Cost-log the spend (instance key — system-driven bootstrap).
            cost_log.record(
                supabase,
                user_id=None,
                purpose=BOOTSTRAP_PURPOSE,
                result=llm_result,
                metadata={"job_posting_id": jid, "target_id": target.id},
            )
            return {"job_id": jid, "target_id": target.id, "clean_score": fit.fit_score}

        graded = await asyncio.gather(*(_grade(jid) for jid in pending))
        for row in graded:
            if row is not None:
                labels.append(row)
                done_pairs.add((row["job_id"], row["target_id"]))
                newly_written += 1
        # Persist after each target so an interruption keeps everything so far.
        _write_labels(out_path, labels)
        logger.info(
            "✓ %s — graded %d/%d (total labels on disk: %d)",
            target.label,
            sum(1 for r in graded if r is not None),
            len(pending),
            len(labels),
        )

    if dry_run:
        logger.info("Dry run — nothing graded, nothing written.")
    else:
        logger.info(
            "Done — wrote %d new label(s) to %s (%d total)", newly_written, out_path, len(labels)
        )
    return newly_written


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap clean LLM fit labels (#60, Phase 2)")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/fixtures/prescan_clean_labels.json"),
        help="JSON output path for (job_id, target_id, clean_score) labels.",
    )
    parser.add_argument(
        "--per-target",
        type=int,
        default=250,
        help="Stratified sample size per target (default 250).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Parallel LLM grade calls (default 5).",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Restrict to a single target id.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report sampled counts per target without grading (spends nothing).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="RNG seed for reproducible stratified sampling.",
    )
    parser.add_argument(
        "--top-band-fraction",
        type=float,
        default=_DEFAULT_TOP_BAND_FRACTION,
        help=(
            "Fraction of each target's sample taken from the top keyword-score "
            "quartile, where genuine fits concentrate "
            f"(default {_DEFAULT_TOP_BAND_FRACTION}; 0 = pure even-band)."
        ),
    )
    args = parser.parse_args()
    asyncio.run(
        bootstrap(
            out_path=args.out,
            per_target=args.per_target,
            concurrency=args.concurrency,
            target_id=args.target,
            dry_run=args.dry_run,
            seed=args.seed,
            top_band_fraction=args.top_band_fraction,
        )
    )


if __name__ == "__main__":
    main()
