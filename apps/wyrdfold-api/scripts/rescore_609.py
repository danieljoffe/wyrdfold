"""#609 one-off rescore — retire the score walls on both bands.

Two populations, two pure-CPU recomputations (no LLM spend):

  graded    scores rows with ``axis_scores`` — stored ``score`` becomes the
            deterministic default-weight axis blend (what the weighted
            display reproduces), replacing the model's holistic number
            (prod evidence: axes {85,55,88,80} stored as 100; 326 rows
            >= 90 walled the graded band).
  ungraded  scores rows without ``axis_scores`` — stored ``score`` and
            ``score_breakdown`` recomputed under the capped-credit keyword
            formula (pre-#609, per-item awards reached ~16x the assumed
            max and ~9,000 rows clamped to 90-100).

Default is a DRY RUN that prints before/after distributions plus the
user-floor impact (how many graded rows fall below the min-score floor
users have configured); ``--apply`` writes. Writes are per-row UPDATEs,
throttled, with the 57014 backoff-retry opted in — safe to run while the
poller is active, though off-peak is kinder to the small instance.

    railway run uv run --package wyrdfold-api python scripts/rescore_609.py
    railway run uv run --package wyrdfold-api python scripts/rescore_609.py --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.targets import AxisWeights, ScoringProfile
from app.services.fit.axis_weights import display_score_from_axes
from app.services.jd_parser import parse_jd
from app.services.scoring import score_job_with_profile
from app.services.supabase_retry import execute_with_retry_sync
from app.services.targets.crud import _parse_target
from app.supabase_pool import create_service_client

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rescore-609")

PAGE = 500
JOB_CHUNK = 150  # in_() URL-length discipline (see insights _IN_CHUNK)


def _bucket(score: int) -> str:
    lo = min(90, (score // 10) * 10)
    return f"{lo}-{lo + 9 if lo < 90 else 100}"


def _hist(counter: Counter[str]) -> str:
    return "  ".join(f"{b}:{counter.get(b, 0)}" for b in sorted(counter, key=lambda x: int(x.split('-')[0])))


def _pages(supabase: Any, select: str, graded: bool):
    """Keyset-paginate scores rows by id, split by gradedness."""
    after: str | None = None
    while True:
        query = supabase.table("scores").select(select).order("id").limit(PAGE)
        query = query.not_.is_("axis_scores", "null") if graded else query.is_("axis_scores", "null")
        if after is not None:
            query = query.gt("id", after)
        resp = execute_with_retry_sync(
            query.execute, label="rescore609/page", retry_statement_timeout=True
        )
        rows = cast(list[dict[str, Any]], resp.data or [])
        if not rows:
            return
        yield rows
        if len(rows) < PAGE:
            return
        after = rows[-1]["id"]


def _apply_update(supabase: Any, row_id: str, payload: dict[str, Any], sleep_ms: int) -> None:
    execute_with_retry_sync(
        supabase.table("scores").update(payload).eq("id", row_id).execute,
        label="rescore609/update",
        retry_statement_timeout=True,
    )
    time.sleep(sleep_ms / 1000)


def rescore_graded(supabase: Any, *, apply: bool, sleep_ms: int) -> tuple[int, int]:
    weights = AxisWeights()
    before: Counter[str] = Counter()
    after_hist: Counter[str] = Counter()
    seen = changed = 0
    for rows in _pages(supabase, "id, score, axis_scores", graded=True):
        for r in rows:
            seen += 1
            old = int(r["score"])
            new = display_score_from_axes(r["axis_scores"], weights)
            before[_bucket(old)] += 1
            after_hist[_bucket(new)] += 1
            if new != old:
                changed += 1
                if apply:
                    _apply_update(
                        supabase, r["id"], {"score": new, "recency_score": new}, sleep_ms
                    )
        log.info("graded: seen=%d changed=%d", seen, changed)
    log.info("graded BEFORE  %s", _hist(before))
    log.info("graded AFTER   %s", _hist(after_hist))
    floor_40 = sum(v for k, v in after_hist.items() if int(k.split("-")[0]) < 40)
    log.info(
        "graded rows below a 40 floor after rescore: %d of %d "
        "(the min-score floor only applies to graded rows — pending is exempt, #47)",
        floor_40,
        seen,
    )
    return seen, changed


def _load_target(supabase: Any, target_id: str, cache: dict[str, Any]) -> Any:
    if target_id not in cache:
        resp = execute_with_retry_sync(
            supabase.table("targets").select("*").eq("id", target_id).limit(1).execute,
            label="rescore609/target",
            retry_statement_timeout=True,
        )
        rows = cast(list[dict[str, Any]], resp.data or [])
        cache[target_id] = _parse_target(rows[0]) if rows else None
    return cache[target_id]


def _iter_recomputed(supabase: Any, rows: list[dict[str, Any]], targets: dict[str, Any]):
    """Yield ``(row, keyword ScoreResult)`` for one page of scores rows.

    Shared by the ungraded rescore and the graded breakdown refresh: fetches
    the page's jobs in chunks, caches parsed JDs per unique job, and skips
    rows whose target/job is gone or whose target has no ScoringProfile.
    """
    job_ids = sorted({r["job_posting_id"] for r in rows})
    jobs: dict[str, dict[str, Any]] = {}
    for i in range(0, len(job_ids), JOB_CHUNK):
        chunk = job_ids[i : i + JOB_CHUNK]
        resp = execute_with_retry_sync(
            supabase.table("jobs")
            .select("id, title, description_html")
            .in_("id", chunk)
            .execute,
            label="rescore609/jobs",
            retry_statement_timeout=True,
        )
        for j in cast(list[dict[str, Any]], resp.data or []):
            jobs[j["id"]] = j

    parsed_cache: dict[str, Any] = {}
    for r in rows:
        target = _load_target(supabase, r["target_id"], targets)
        job = jobs.get(r["job_posting_id"])
        if target is None or job is None:
            yield r, None
            continue
        profile = target.scoring_profile
        if not isinstance(profile, ScoringProfile):
            yield r, None
            continue
        jid = job["id"]
        if jid not in parsed_cache:
            parsed_cache[jid] = parse_jd(job.get("description_html") or "")
        yield (
            r,
            score_job_with_profile(
                job.get("title") or "",
                job.get("description_html") or "",
                profile,
                parsed_jd=parsed_cache[jid],
                search_keywords=target.search_keywords,
            ),
        )


def rescore_ungraded(supabase: Any, *, apply: bool, sleep_ms: int) -> tuple[int, int]:
    before: Counter[str] = Counter()
    after_hist: Counter[str] = Counter()
    targets: dict[str, Any] = {}
    seen = changed = skipped = 0
    for rows in _pages(
        supabase, "id, job_posting_id, target_id, score", graded=False
    ):
        for r, result in _iter_recomputed(supabase, rows, targets):
            seen += 1
            if result is None:
                skipped += 1
                continue
            old = int(r["score"])
            before[_bucket(old)] += 1
            after_hist[_bucket(result.score)] += 1
            if result.score != old:
                changed += 1
                if apply:
                    _apply_update(
                        supabase,
                        r["id"],
                        {
                            "score": result.score,
                            "recency_score": result.score,
                            "score_breakdown": result.breakdown.model_dump(),
                        },
                        sleep_ms,
                    )
        log.info("ungraded: seen=%d changed=%d skipped=%d", seen, changed, skipped)
    log.info("ungraded BEFORE %s", _hist(before))
    log.info("ungraded AFTER  %s", _hist(after_hist))
    return seen, changed


def refresh_graded_breakdowns(
    supabase: Any, *, apply: bool, sleep_ms: int
) -> tuple[int, int]:
    """Rewrite ONLY ``score_breakdown`` for graded rows, on the normalized scale.

    The graded rescore replaces ``score`` with the axis blend but the stored
    keyword ``score_breakdown`` predates #609 — raw internal sums ("+124.2")
    that no longer relate to anything on screen. Recompute them under the
    capped-credit formula so the keyword components are percentage points
    (they sum to the row's keyword percentage — the axis-blend ``score``
    itself is untouched here). The panel showing fit AXES for graded rows
    instead of keyword components is the display follow-up tracked on #609.
    """
    targets: dict[str, Any] = {}
    seen = changed = skipped = 0
    for rows in _pages(supabase, "id, job_posting_id, target_id, score", graded=True):
        for r, result in _iter_recomputed(supabase, rows, targets):
            seen += 1
            if result is None:
                skipped += 1
                continue
            changed += 1
            if apply:
                _apply_update(
                    supabase,
                    r["id"],
                    {"score_breakdown": result.breakdown.model_dump()},
                    sleep_ms,
                )
        log.info(
            "graded-breakdowns: seen=%d rewritten=%d skipped=%d", seen, changed, skipped
        )
    return seen, changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--graded-only", action="store_true")
    ap.add_argument("--ungraded-only", action="store_true")
    ap.add_argument(
        "--graded-breakdowns",
        action="store_true",
        help="ONLY refresh graded rows' score_breakdown to the normalized scale",
    )
    ap.add_argument("--sleep-ms", type=int, default=120, help="pause between row updates")
    args = ap.parse_args()

    supabase = create_service_client()
    if supabase is None:
        raise RuntimeError("Supabase not configured")

    mode = "APPLY" if args.apply else "DRY-RUN"
    log.info("rescore-609 %s", mode)
    if args.graded_breakdowns:
        refresh_graded_breakdowns(supabase, apply=args.apply, sleep_ms=args.sleep_ms)
        log.info("done (%s)", mode)
        return
    if not args.ungraded_only:
        rescore_graded(supabase, apply=args.apply, sleep_ms=args.sleep_ms)
    if not args.graded_only:
        rescore_ungraded(supabase, apply=args.apply, sleep_ms=args.sleep_ms)
    log.info("done (%s)", mode)


if __name__ == "__main__":
    main()
