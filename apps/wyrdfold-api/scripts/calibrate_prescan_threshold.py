"""Calibrate the per-target pre-scan cosine threshold (#60, Phase 2).

Given the clean-label file from ``bootstrap_clean_labels.py`` plus the job
vectors in ``job_embeddings`` (Phase 1) and the target vectors in
``targets.embedding`` (Phase 2 backfill), this computes, per target,
``cosine(job_vec, target_vec)`` for every labelled job and chooses a
``prescan_cosine_threshold`` that keeps ~95% recall of the clean-positive jobs
(see ``app/services/embeddings/prescan_calibration.py`` for the policy). It
prints a per-target report (threshold, recall, off-domain leakage, counts).

DRY-RUN BY DEFAULT: it writes NOTHING to the DB unless ``--write`` is passed.
``--write`` updates ``targets.prescan_cosine_threshold`` — that is the only
mutation, and it is the step that arms the Phase 3 gate, so it is deliberately
opt-in.

This can't fully run until BOTH inputs exist: job vectors (Phase 1 backfill) and
target vectors (Phase 2 backfill), plus the clean-label file (bootstrap). With
any of those missing it reports which jobs/targets it couldn't resolve and
skips them.

Usage:
    cd apps/wyrdfold-api && uv run python scripts/calibrate_prescan_threshold.py \
        --labels tests/fixtures/prescan_clean_labels.json            # report only (no write)
    cd apps/wyrdfold-api && railway run uv run python scripts/calibrate_prescan_threshold.py \
        --labels tests/fixtures/prescan_clean_labels.json --write    # arm the gate
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
from pathlib import Path
from typing import Any, cast

from app.services.embeddings.job_embeddings import DEFAULT_MODEL
from app.services.embeddings.prescan_calibration import (
    CalibrationResult,
    calibrate_threshold,
    cosine,
)
from app.supabase_pool import get_supabase_pool, init_supabase

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("calibrate_prescan")

_PAGE = 1000


def _parse_vector(raw: Any) -> list[float] | None:
    """Coerce a pgvector cell into a list[float].

    PostgREST returns a ``vector`` column either as a JSON array (list) or as its
    text form ``"[0.1,0.2,...]"`` depending on client/version — handle both.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
        if isinstance(parsed, (list, tuple)):
            return [float(x) for x in parsed]
    return None


def _load_labels(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return cast(list[dict[str, Any]], data.get("labels", []))


def _fetch_target_vectors(supabase: Any) -> dict[str, list[float]]:
    """All targets that have an embedding, keyed by id."""
    out: dict[str, list[float]] = {}
    offset = 0
    while True:
        resp = (
            supabase.table("targets")
            .select("id, label, embedding")
            .range(offset, offset + _PAGE - 1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], resp.data or [])
        if not rows:
            break
        for row in rows:
            vec = _parse_vector(row.get("embedding"))
            if vec is not None:
                out[row["id"]] = vec
        if len(rows) < _PAGE:
            break
        offset += _PAGE
    return out


def _fetch_job_vectors(supabase: Any, job_ids: list[str], *, model: str) -> dict[str, list[float]]:
    """Job vectors for the given ids at ``model``, keyed by job id."""
    out: dict[str, list[float]] = {}
    for i in range(0, len(job_ids), _PAGE):
        chunk = job_ids[i : i + _PAGE]
        resp = (
            supabase.table("job_embeddings")
            .select("job_posting_id, embedding")
            .eq("model", model)
            .in_("job_posting_id", chunk)
            .execute()
        )
        for row in cast(list[dict[str, Any]], resp.data or []):
            vec = _parse_vector(row.get("embedding"))
            if vec is not None:
                out[row["job_posting_id"]] = vec
    return out


def _report_line(label: str, target_id: str, res: CalibrationResult, missing: int) -> str:
    note = f"  [{res.note}]" if res.note else ""
    miss = f", {missing} job-vec missing" if missing else ""
    return (
        f"{label} ({target_id}): threshold={res.threshold:.4f} "
        f"recall={res.recall:.2%} leakage={res.leakage:.2%} "
        f"(+{res.n_positive}/-{res.n_negative}, {res.n_labels} labelled{miss}){note}"
    )


def calibrate(
    *,
    labels_path: Path,
    write: bool,
    model: str,
    positive_cutoff: float,
    target_recall: float,
) -> dict[str, CalibrationResult]:
    init_supabase()
    supabase = get_supabase_pool()
    if supabase is None:
        raise SystemExit("Supabase not configured (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)")

    labels = _load_labels(labels_path)
    if not labels:
        raise SystemExit(f"No labels in {labels_path} — run bootstrap_clean_labels.py first.")

    # Group labels by target.
    by_target: dict[str, list[dict[str, Any]]] = {}
    for row in labels:
        by_target.setdefault(row["target_id"], []).append(row)

    target_vecs = _fetch_target_vectors(supabase)
    all_job_ids = sorted({row["job_id"] for row in labels})
    job_vecs = _fetch_job_vectors(supabase, all_job_ids, model=model)

    # Pull target labels for the report (best-effort; falls back to the id).
    label_by_id: dict[str, str] = {}
    resp = supabase.table("targets").select("id, label").execute()
    for row in cast(list[dict[str, Any]], resp.data or []):
        label_by_id[row["id"]] = row.get("label") or row["id"]

    logger.info(
        "Calibrating %d target(s) from %d label(s) — %d target vec(s), %d job vec(s) resolved.%s",
        len(by_target),
        len(labels),
        len(target_vecs),
        len(job_vecs),
        "" if write else "  [DRY RUN — no DB writes]",
    )

    results: dict[str, CalibrationResult] = {}
    for target_id, rows in sorted(by_target.items()):
        tvec = target_vecs.get(target_id)
        if tvec is None:
            logger.warning(
                "Skipping target %s (%s) — no embedding (run backfill_target_embeddings.py)",
                target_id,
                label_by_id.get(target_id, "?"),
            )
            continue

        cosines_with_labels: list[tuple[float, float]] = []
        missing = 0
        for row in rows:
            jvec = job_vecs.get(row["job_id"])
            if jvec is None:
                missing += 1
                continue
            cosines_with_labels.append((cosine(jvec, tvec), float(row["clean_score"])))

        if not cosines_with_labels:
            logger.warning(
                "Skipping target %s (%s) — 0/%d labelled jobs have a vector",
                target_id,
                label_by_id.get(target_id, "?"),
                len(rows),
            )
            continue

        res = calibrate_threshold(
            cosines_with_labels=cosines_with_labels,
            positive_cutoff=positive_cutoff,
            target_recall=target_recall,
        )
        results[target_id] = res
        logger.info(_report_line(label_by_id.get(target_id, "?"), target_id, res, missing))

        if write:
            supabase.table("targets").update(
                {"prescan_cosine_threshold": res.threshold}
            ).eq("id", target_id).execute()
            logger.info("  → wrote prescan_cosine_threshold=%.4f", res.threshold)

    if write:
        logger.info("Wrote thresholds for %d target(s).", len(results))
    else:
        logger.info("Dry run — computed %d threshold(s), wrote nothing. Pass --write to arm.", len(results))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate pre-scan cosine thresholds (#60, Phase 2)")
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("tests/fixtures/prescan_clean_labels.json"),
        help="Clean-label JSON from bootstrap_clean_labels.py.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write thresholds to targets.prescan_cosine_threshold (DEFAULT: dry-run).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help="Embedding model whose job vectors to read (default voyage-3).",
    )
    parser.add_argument(
        "--positive-cutoff",
        type=float,
        default=70.0,
        help="clean_score at/above which a job counts as a real match (default 70).",
    )
    parser.add_argument(
        "--recall",
        type=float,
        default=0.95,
        help="Minimum clean-positive recall the threshold must keep (default 0.95).",
    )
    args = parser.parse_args()
    calibrate(
        labels_path=args.labels,
        write=args.write,
        model=args.model,
        positive_cutoff=args.positive_cutoff,
        target_recall=args.recall,
    )


if __name__ == "__main__":
    main()
