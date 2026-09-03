"""Catalog-health observability (#958) — product-level eyes on the intake funnel.

The #952 lesson: every infrastructure metric said healthy ("3,957 listings
ingested") while almost none of those listings were relevant to any active
target — the system was product-dead for two days before a human noticed.
This module measures the catalog the way a user experiences it, once per
recorded poll cycle:

* window intake — how much was admitted in the trailing window, and how much
  of it is *relevant* (holds at least one ``scores`` row, i.e. entered some
  target's pipeline rather than only the broad catalog);
* corpus quality — % ungraded, % location-unknown, and the ``role_family``
  histogram, via one server-side pass (``catalog_health_snapshot``);
* the admitted-title token histogram, and a TRIPWIRE comparing it against a
  trailing baseline: when the distribution shifts hard (engineer/developer →
  assistant/specialist was #952's signature), log at WARNING and flag the row.

Everything here is best-effort telemetry: ``record_cycle_health`` must never
break a poll cycle, so the poller calls it through a broad try/except and a
failure costs one cycle's row, nothing else.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any, cast

from supabase import AsyncClient

from app.config import settings
from app.services.job_search import _tokenize

logger = logging.getLogger(__name__)

# The window fetch pages deterministically (cataloged_at, id) instead of
# trusting one bounded select: the intake ceiling is HOURLY, so a 24h window
# can legitimately hold hourly-cap x 24 rows — and a firehose is exactly the
# regime this telemetry must describe honestly, not silently sample. The page
# backstop sits above that physical maximum; if it is still hit, the row says
# so (window_truncated) and the tripwire refuses to judge a partial sample.
_WINDOW_PAGE = 1000
_WINDOW_MAX_PAGES = 20

# ``scores`` membership lookups chunk the id list — PostgREST URLs 414 when an
# ``in_()`` carries more than ~150-200 ids (#57).
_SCORES_CHUNK = 150

# Tokens shorter than this, or purely numeric, are connective noise ("of",
# "sr", req numbers) rather than role vocabulary; the tripwire compares role
# vocabulary.
_MIN_TOKEN_LEN = 3


def tokenize_titles(titles: list[str]) -> Counter[str]:
    """Token histogram for a batch of admitted titles.

    Rides the search sanitizer (lowercase, punctuation stripped, per-title
    dedupe) so "Sr. Frontend Engineer!" and "frontend engineer" count the
    same vocabulary, then drops short/numeric tokens.
    """
    counts: Counter[str] = Counter()
    for title in titles:
        for tok in _tokenize(title or ""):
            if len(tok) >= _MIN_TOKEN_LEN and not tok.isdigit():
                counts[tok] += 1
    return counts


def tv_distance(current: Counter[str], baseline: Counter[str]) -> float:
    """Total-variation distance between two normalized token distributions.

    0.0 = identical mix, 1.0 = fully disjoint vocabularies. Half the L1
    distance over the union, so every token counts once.
    """
    cur_total = sum(current.values())
    base_total = sum(baseline.values())
    if cur_total == 0 or base_total == 0:
        return 1.0
    tokens = set(current) | set(baseline)
    return 0.5 * sum(
        abs(current[t] / cur_total - baseline[t] / base_total) for t in tokens
    )


def evaluate_tripwire(
    current: Counter[str],
    baseline: Counter[str],
    *,
    threshold: float,
    min_titles: int,
    current_titles: int,
    baseline_titles: int,
) -> tuple[bool, float | None, str | None]:
    """(fired, distance, reason) for the window-vs-baseline comparison.

    The sample floor counts TITLES (jobs), passed in explicitly — token
    occurrences would let ten ordinary titles impersonate a 30-strong sample
    and one absurd title satisfy the floor alone. Below the floor on either
    side it records WHY it did not evaluate instead of a verdict — a tripwire
    that fires on a 3-job Sunday window would train the operator to ignore
    it. Both distributions must be truncated the SAME way by the caller
    (top-N vs top-N): the persisted baseline is top-N by construction, and
    comparing it against an untruncated current would read a stable broad
    vocabulary as a shift.
    """
    if current_titles < min_titles:
        return False, None, f"window sample too small ({current_titles} titles < {min_titles})"
    if baseline_titles < min_titles:
        return False, None, f"baseline too small ({baseline_titles} titles < {min_titles})"
    distance = tv_distance(current, baseline)
    if distance > threshold:
        return True, distance, f"token distribution shifted (tv={distance:.3f} > {threshold})"
    return False, distance, None


def _median_age_hours(rows: list[dict[str, Any]]) -> float | None:
    """Median (cataloged_at - source_posted_at) in hours, over rows that
    carry both. None when nothing is datable — never 0, which would read as
    "everything is fresh"."""
    ages: list[float] = []
    for r in rows:
        posted, cataloged = r.get("source_posted_at"), r.get("cataloged_at")
        if not posted or not cataloged:
            continue
        try:
            delta = datetime.fromisoformat(cataloged) - datetime.fromisoformat(posted)
        except ValueError:
            continue
        if delta.total_seconds() >= 0:
            ages.append(delta.total_seconds() / 3600)
    return round(median(ages), 1) if ages else None


async def _relevant_count(supabase: AsyncClient, job_ids: list[str]) -> int:
    """How many of *job_ids* hold at least one ``scores`` row."""
    relevant: set[str] = set()
    for i in range(0, len(job_ids), _SCORES_CHUNK):
        chunk = job_ids[i : i + _SCORES_CHUNK]
        resp = await (
            supabase.table("scores").select("job_posting_id").in_("job_posting_id", chunk).execute()
        )
        for row in cast(list[dict[str, Any]], resp.data or []):
            relevant.add(str(row["job_posting_id"]))
    return len(relevant)


async def _recorded_recently(supabase: AsyncClient, now: datetime) -> bool:
    if settings.catalog_health_min_interval_minutes <= 0:
        return False
    resp = await (
        supabase.table("catalog_health_cycles")
        .select("computed_at")
        .order("computed_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return False
    last = datetime.fromisoformat(str(rows[0]["computed_at"]))
    return (now - last) < timedelta(minutes=settings.catalog_health_min_interval_minutes)


async def _baseline_tokens(
    supabase: AsyncClient, window_start: datetime
) -> tuple[Counter[str], int]:
    """(final top-N baseline histogram, total titles) from prior rows whose
    windows ended before this one began — zero overlap with the current
    window, so the comparison is honest.

    The returned counter is truncated to the SAME top-N the current side
    uses. Each historical row stores only its own top-N, but the union
    across up to ``baseline_cycles`` rows can span hundreds of tokens as
    ranks ~10-20 rotate in and out of each cycle's cut — comparing the
    current top-N against that wide union reads a perfectly stable
    population as a shift (the second asymmetry the #974 review caught).
    Titles ride each row's ``new_jobs`` so the tripwire's sample floor
    counts jobs, not token occurrences.
    """
    resp = await (
        supabase.table("catalog_health_cycles")
        .select("top_title_tokens, new_jobs")
        .lte("computed_at", window_start.isoformat())
        .order("computed_at", desc=True)
        .limit(settings.catalog_health_baseline_cycles)
        .execute()
    )
    aggregate: Counter[str] = Counter()
    titles = 0
    for row in cast(list[dict[str, Any]], resp.data or []):
        titles += int(row.get("new_jobs") or 0)
        for pair in row.get("top_title_tokens") or []:
            try:
                token, count = pair[0], int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            aggregate[str(token)] += count
    baseline: Counter[str] = Counter(
        dict(aggregate.most_common(settings.catalog_health_top_tokens))
    )
    return baseline, titles


async def _window_rows(
    supabase: AsyncClient, window_start: datetime
) -> tuple[list[dict[str, Any]], int, bool]:
    """(collected rows, exact count, truncated) for the intake window.

    The filters mirror the corpus gate ``catalog_health_snapshot`` uses
    (live + US: archived/purged NULL, is_us IS NOT FALSE) so the window
    metrics and the corpus percentages beside them describe the same
    catalog. Exact count first, then deterministic pages — ``new_jobs``
    stays the real count even when the page backstop truncates collection.
    """

    def _filtered(query: Any) -> Any:
        return (
            query.gte("cataloged_at", window_start.isoformat())
            .is_("archived_at", "null")
            .is_("purged_at", "null")
            .not_.is_("is_us", "false")
        )

    count_resp = await _filtered(
        supabase.table("jobs").select("id", count="exact", head=True)  # type: ignore[arg-type]
    ).execute()
    exact = int(getattr(count_resp, "count", None) or 0)

    rows: list[dict[str, Any]] = []
    for page in range(_WINDOW_MAX_PAGES):
        resp = await (
            _filtered(supabase.table("jobs").select("id, title, cataloged_at, source_posted_at"))
            .order("cataloged_at", desc=False)
            .order("id", desc=False)
            .range(page * _WINDOW_PAGE, (page + 1) * _WINDOW_PAGE - 1)
            .execute()
        )
        batch = cast(list[dict[str, Any]], resp.data or [])
        rows.extend(batch)
        if len(batch) < _WINDOW_PAGE:
            break
    return rows, exact, len(rows) < exact


async def record_cycle_health(supabase: AsyncClient) -> dict[str, Any] | None:
    """Measure and persist one catalog-health row; returns it for the cycle
    log line, or None when disabled/throttled/failed.

    Best-effort by contract: raises nothing (the poller still wraps the call,
    belt and braces). One failed cycle costs one row of telemetry.
    """
    if not settings.catalog_health_enabled:
        return None
    try:
        now = datetime.now(UTC)
        if await _recorded_recently(supabase, now):
            return None
        window_start = now - timedelta(hours=settings.catalog_health_window_hours)

        window_rows, exact_count, truncated = await _window_rows(supabase, window_start)

        tokens = tokenize_titles([str(r.get("title") or "") for r in window_rows])
        top_pairs = tokens.most_common(settings.catalog_health_top_tokens)
        # Symmetric comparison: the baseline is reconstructed from persisted
        # top-N histograms, so the current side must be truncated to the SAME
        # top-N — full-vs-truncated would read a stable broad vocabulary as a
        # shift (the long tail vanishing from one side only).
        current_top: Counter[str] = Counter(dict(top_pairs))
        baseline, baseline_titles = await _baseline_tokens(supabase, window_start)
        fired: bool
        distance: float | None
        reason: str | None
        if truncated:
            fired, distance = False, None
            reason = (
                f"window truncated ({len(window_rows)} of {exact_count} rows "
                "collected) — refusing to judge a partial sample"
            )
        else:
            fired, distance, reason = evaluate_tripwire(
                current_top,
                baseline,
                threshold=settings.catalog_health_tripwire_threshold,
                min_titles=settings.catalog_health_min_sample_titles,
                current_titles=len(window_rows),
                baseline_titles=baseline_titles,
            )

        snapshot_resp = await supabase.rpc("catalog_health_snapshot", {}).execute()
        snapshot = cast(dict[str, Any], snapshot_resp.data or {})
        live_total = int(snapshot.get("live_total") or 0)

        def _pct(part: Any) -> float | None:
            return round(100 * int(part or 0) / live_total, 2) if live_total else None

        row: dict[str, Any] = {
            "computed_at": now.isoformat(),
            "window_started_at": window_start.isoformat(),
            # The EXACT count, even when collection truncated — never report
            # min(actual, backstop) as though it were the real intake.
            "new_jobs": exact_count,
            "window_truncated": truncated,
            "relevant_jobs": await _relevant_count(
                supabase, [str(r["id"]) for r in window_rows]
            ),
            "live_total": live_total,
            "pct_ungraded": _pct(snapshot.get("ungraded")),
            "pct_location_unknown": _pct(snapshot.get("location_unknown")),
            "family_counts": snapshot.get("family_counts") or {},
            "median_admission_age_hours": _median_age_hours(window_rows),
            "top_title_tokens": [[t, c] for t, c in top_pairs],
            "tripwire_fired": fired,
            "tripwire_distance": round(distance, 3) if distance is not None else None,
            "tripwire_reason": reason,
        }
        await supabase.table("catalog_health_cycles").insert(row).execute()

        # Prune beyond retention — 48 rows/day, so this stays instant.
        cutoff = now - timedelta(days=settings.catalog_health_retention_days)
        await (
            supabase.table("catalog_health_cycles")
            .delete()
            .lt("computed_at", cutoff.isoformat())
            .execute()
        )

        if fired:
            logger.warning(
                "catalog_health_tripwire FIRED: %s — window top tokens %s vs a "
                "baseline of %d tokens; the intake mix changed regime (#952 class)",
                reason,
                row["top_title_tokens"][:5],
                sum(baseline.values()),
            )
        logger.info(
            "catalog_health window=%dh new=%d relevant=%d live=%d ungraded=%s%% "
            "loc_unknown=%s%% tripwire=%s",
            settings.catalog_health_window_hours,
            row["new_jobs"],
            row["relevant_jobs"],
            live_total,
            row["pct_ungraded"],
            row["pct_location_unknown"],
            "FIRED"
            if fired
            else (f"tv={row['tripwire_distance']}" if distance is not None else "n/a"),
        )
        return row
    except Exception:
        logger.exception("catalog_health recording failed — telemetry only, cycle unaffected")
        return None
