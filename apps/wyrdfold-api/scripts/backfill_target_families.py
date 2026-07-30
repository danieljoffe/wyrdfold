"""Derive ``role_family`` for targets that predate the off-family gate.

A target with ``role_family IS NULL`` makes the #277 family gate a no-op for
that target on EVERY surface — the ``get_target_jobs`` RPC, the Python
``_gate_off_family``, the membership badge, the Phase-2 admission gate, and
the poll-cycle reconcile all ungate when the target side is unclassified.
Newer targets get a family at creation (``derive_profile_from_label``); this
backfills the stragglers ("All Levels: Fullstack Software Engineer",
"Hadrian - Fullstack Software Engineer" as of 2026-07-30) with the SAME
derive call, writing ONLY ``role_family`` (label, profile, keywords, examples
all stay untouched — no profile_version bump, no re-score).

The ``DerivedTarget`` model's validator coerces any out-of-vocabulary family
to ``None``, so a bad LLM answer degrades to "still unclassified", never to a
corrupt value.

After executing, run ``backfill_offfamily_promising.py`` once: newly
classified targets may hold promising rows their fresh family now
contradicts. (The poll-cycle reconcile also converges them, one cycle at a
time, for re-polled jobs.)

Usage::

    cd apps/wyrdfold-api
    uv run python scripts/backfill_target_families.py            # dry-run
    uv run python scripts/backfill_target_families.py --execute

Env required: ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY`` + a REAL
``LLM_PROVIDER`` (anthropic or openrouter — prod runs openrouter) with its
key; the mock provider is refused on ``--execute``.
Cost: one small derive call per unclassified target (a handful exist).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.services.llm import get_default_client as get_llm
from app.services.llm.cost_log import record as record_llm_cost
from app.services.targets.derive_profile_from_label import (
    DEFAULT_PURPOSE,
    derive_profile_from_label,
)
from app.supabase_pool import get_supabase_pool, init_supabase

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_target_families")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write derived families. Default is a report-only dry-run.",
    )
    args = parser.parse_args()

    if settings.llm_provider == "mock" and args.execute:
        raise SystemExit(
            "ERROR: LLM_PROVIDER is 'mock' — mock verdicts must not reach the "
            "DB. Run with a real provider (prod uses openrouter), or use the "
            "default dry-run for a count-only pass."
        )

    init_supabase()
    supabase = get_supabase_pool()
    if supabase is None:
        raise SystemExit(
            "ERROR: Supabase not configured — check SUPABASE_URL + "
            "SUPABASE_SERVICE_ROLE_KEY in apps/wyrdfold-api/.env"
        )

    rows = cast(
        list[dict[str, Any]],
        supabase.table("targets")
        .select("id, label, is_active")
        .is_("role_family", "null")
        .execute()
        .data
        or [],
    )
    logger.info("targets with role_family NULL: %d", len(rows))
    for r in rows:
        logger.info("  %s  active=%s  %s", cast(str, r["id"])[:8], r.get("is_active"), r.get("label"))
    if not rows:
        return
    if not args.execute:
        logger.info("dry-run — nothing derived or written (pass --execute to apply)")
        return

    llm = get_llm()
    derived_n = skipped_n = 0
    for r in rows:
        label = cast(str, r.get("label") or "")
        if not label.strip():
            logger.warning("  %s has no label — skipping", r["id"])
            skipped_n += 1
            continue
        derived, result = await derive_profile_from_label(llm, label=label)
        if result is not None:
            try:
                record_llm_cost(
                    supabase,
                    user_id=None,
                    purpose=DEFAULT_PURPOSE,
                    result=result,
                    metadata={"target_id": r["id"], "source": "backfill_target_families"},
                )
            except Exception:
                logger.exception("cost log failed")
        family = derived.role_family
        if family is None:
            # Vocabulary validator coerced an off-menu answer — leave NULL.
            logger.warning("  %s (%s): derive returned no usable family", r["id"], label)
            skipped_n += 1
            continue
        supabase.table("targets").update({"role_family": family}).eq("id", r["id"]).execute()
        derived_n += 1
        logger.info("  %s (%s) -> %s", cast(str, r["id"])[:8], label, family)

    logger.info("DONE: derived=%d skipped=%d", derived_n, skipped_n)
    logger.info("Now run: uv run python scripts/backfill_offfamily_promising.py --execute")


if __name__ == "__main__":
    asyncio.run(main())
