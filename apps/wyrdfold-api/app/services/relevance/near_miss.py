"""Near-miss mining over the Phase-1 rejection store.

Zero-LLM insight extraction (plan-phase2-structured-harvest.md §free
mining): the triage gate already paid deepseek for every rejection in
``phase1_rejections``, and since the store went persistent (#703) those
verdicts carry per-title confidence. A rejection the model itself marked
shaky — confidence under :data:`NEAR_MISS_CONFIDENCE_CEILING` — is a
role *adjacent* to the target. Surfacing them per target answers "what
am I telling the gate to exclude that it wasn't sure about?": either
postings the user wants the target widened to include, or evidence the
target label reads narrower than intended. Measured 2026-08-13: ~2.5%
of the standing corpus sits under the ceiling, and the band is exactly
the adjacent-role story ("staff software engineer - cloud" at 60 for a
full-stack target).

Access model: ``phase1_rejections`` is service-role-only (RLS, no
policies), so reads here take the SERVICE client — bounded by target
rows the ROUTER resolved through the caller's JWT client (the #557 §2
discipline: a service read may never be keyed by caller-supplied ids
that weren't first proven to belong to the caller).

Only CURRENT ``profile_version`` rows count: a rejection judged under an
older profile says nothing about the target as it exists now.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from supabase import AsyncClient

from app.models.insights import NearMissInsights, NearMissTitle, TargetNearMisses

logger = logging.getLogger(__name__)

# A rejection at or above this confidence is a settled "no" — not insight.
# Data-derived (2026-08-13 corpus): <80 selects ~2.5% of verdicts, and the
# band reads as genuinely adjacent titles rather than noise.
NEAR_MISS_CONFIDENCE_CEILING = 80

# Only recent judgments: a near-miss on a posting that has long since
# closed is not actionable. Bounded well inside the store's 60d TTL.
NEAR_MISS_WINDOW_DAYS = 30

# Shakiest-first per target; a screenful, not a dump.
NEAR_MISS_LIMIT_PER_TARGET = 20


async def compute_near_misses(
    service: AsyncClient,
    targets: list[tuple[str, str, int]],
) -> NearMissInsights:
    """Low-confidence rejections per target, shakiest verdicts first.

    ``targets`` is ``(target_id, label, profile_version)`` tuples the
    caller resolved through the user's OWN client — that resolution is
    the authorization boundary for the service-role read below.

    Per-target failures degrade to an empty list rather than failing the
    whole response — insights are advisory, never load-bearing.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=NEAR_MISS_WINDOW_DAYS)).isoformat()
    out: list[TargetNearMisses] = []
    for target_id, label, profile_version in targets:
        titles: list[NearMissTitle] = []
        try:
            resp = await (
                service.table("phase1_rejections")
                .select("title_norm, confidence, judged_at")
                .eq("target_id", target_id)
                .eq("profile_version", profile_version)
                .lt("confidence", NEAR_MISS_CONFIDENCE_CEILING)
                .gte("judged_at", cutoff)
                .order("confidence")
                .order("judged_at", desc=True)
                .limit(NEAR_MISS_LIMIT_PER_TARGET)
                .execute()
            )
            titles = [
                NearMissTitle(
                    title=row["title_norm"],
                    confidence=row["confidence"],
                    last_judged_at=row["judged_at"],
                )
                for row in cast(list[dict[str, Any]], resp.data or [])
                if row.get("confidence") is not None
            ]
        except Exception:
            logger.warning(
                "near-miss read failed for target %s — returning empty for it",
                target_id,
                exc_info=True,
            )
        out.append(TargetNearMisses(target_id=target_id, label=label, titles=titles))
    return NearMissInsights(
        targets=out,
        confidence_ceiling=NEAR_MISS_CONFIDENCE_CEILING,
        window_days=NEAR_MISS_WINDOW_DAYS,
    )
