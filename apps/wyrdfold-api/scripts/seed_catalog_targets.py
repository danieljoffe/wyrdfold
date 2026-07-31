"""Seed the app-owned catalog targets that drive corpus ingestion breadth.

The app-owned targets model: ``targets`` is the shared catalog and the app
collects listings for every active entry; ``user_targets`` merely attributes
entries to users. This script creates the generic catalog entries whose
families the corpus was missing (2026-07-30: one active customer_experience
target → 15 jobs/day; the payer-fallback PR makes unsponsored targets triage
on the instance key).

Per label: exact ``normalized_label`` find-or-create (``crud.create``), then
the SAME derive-persist mapping as the user flow (``derive_manual_target_bg``)
via ``derive_profile_from_label`` + ``crud.update``, then ``app_active=True``
— the standing instance-sponsorship floor (schema audit P0, 2026-07-31).
``app_active`` is never written by user actions, so catalog rows stay in the
pipeline no matter what memberships come and go; Phase-2/alerts still never
run for them (both key off active *memberships*), and per-user surfaces are
unaffected.

HARD GUARD — never adopt a user's row: if a label resolves (by exact
normalized label) to a target that has ANY ``user_targets`` links, it is
SKIPPED loudly. Users keep their targets exactly as they left them; only
link-free rows are created or (re)activated. Deliberately NO fuzzy matching
(``find_matching_target``) here for the same reason.

Idempotent: re-running skips rows that are already active with a profile.

Usage::

    cd apps/wyrdfold-api
    uv run python scripts/seed_catalog_targets.py             # dry-run
    uv run python scripts/seed_catalog_targets.py --execute

Env required: ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY`` + a REAL
``LLM_PROVIDER`` (anthropic or openrouter) with its key; mock is refused on
``--execute``. Cost: one derive call per new label (~cents).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, cast

from supabase import Client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.models.targets import TargetCreate, TargetUpdate
from app.services.llm import get_default_client as get_llm
from app.services.llm.cost_log import record as record_llm_cost
from app.services.targets import crud
from app.services.targets.derive_profile_from_label import (
    DEFAULT_PURPOSE,
    derive_profile_from_label,
)
from app.supabase_pool import get_supabase_pool, init_supabase

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed_catalog_targets")

CATALOG_LABELS = (
    "Software Engineer",
    "Product Manager",
    "Data Scientist / ML Engineer",
    "Product Designer",
    "DevOps / SRE Engineer",
)


def _user_link_count(supabase: Client, target_id: str) -> int:
    resp = (
        supabase.table("user_targets")
        .select("id", count="exact", head=True)
        .eq("target_id", target_id)
        .execute()
    )
    return cast(int, resp.count or 0)


async def _seed_one(supabase: Client, llm: Any, label: str, *, execute: bool) -> str:
    """Returns a one-word outcome for the summary."""
    existing = crud.get_by_normalized_label(supabase, crud.normalize_label(label))

    if existing is not None:
        links = _user_link_count(supabase, existing.id)
        if links > 0:
            logger.warning(
                "  SKIP %r — resolves to %s with %d user link(s); "
                "never adopt a user's row",
                label,
                existing.id[:8],
                links,
            )
            return "skipped_user_row"
        if existing.app_active and existing.role_family is not None:
            logger.info("  OK   %r — already an active catalog row (%s)", label, existing.id[:8])
            return "already_active"

    if not execute:
        state = "exists, will activate" if existing is not None else "will create+derive"
        logger.info("  PLAN %r — %s", label, state)
        return "planned"

    target = existing or crud.create(supabase, payload=TargetCreate(label=label))

    if target.role_family is None or not (target.search_keywords or []):
        derived, result = await derive_profile_from_label(llm, label=label)
        if result is not None:
            try:
                record_llm_cost(
                    supabase,
                    user_id=None,  # instance spend — catalog work
                    purpose=DEFAULT_PURPOSE,
                    result=result,
                    metadata={"target_id": target.id, "source": "seed_catalog_targets"},
                )
            except Exception:
                logger.exception("cost log failed")
        updated = crud.update(
            supabase,
            target.id,
            TargetUpdate(
                scoring_profile=derived.scoring_profile,
                search_keywords=derived.search_keywords,
                example_promising_titles=derived.example_promising_titles,
                example_unpromising_titles=derived.example_unpromising_titles,
                description=derived.description,
                seniority_hint=derived.seniority_hint,
                domain_hints=derived.domain_hints or None,
                role_family=derived.role_family,
                activation_status="idle",
                app_active=True,
            ),
        )
    else:
        updated = crud.update(supabase, target.id, TargetUpdate(app_active=True))

    if updated is None:
        logger.error("  FAIL %r — update returned no row (%s)", label, target.id[:8])
        return "failed"
    logger.info(
        "  DONE %r -> %s (family=%s, %d keywords)",
        label,
        updated.id[:8],
        updated.role_family,
        len(updated.search_keywords or []),
    )
    return "seeded"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create/derive/activate. Default is a report-only dry-run.",
    )
    args = parser.parse_args()

    if settings.llm_provider == "mock" and args.execute:
        raise SystemExit(
            "ERROR: LLM_PROVIDER is 'mock' — mock profiles must not reach the "
            "DB. Run with a real provider (prod uses openrouter)."
        )

    init_supabase()
    supabase = get_supabase_pool()
    if supabase is None:
        raise SystemExit(
            "ERROR: Supabase not configured — check SUPABASE_URL + "
            "SUPABASE_SERVICE_ROLE_KEY in apps/wyrdfold-api/.env"
        )

    llm = get_llm() if args.execute else None
    logger.info("catalog labels: %d  execute=%s", len(CATALOG_LABELS), args.execute)
    outcomes: dict[str, int] = {}
    for label in CATALOG_LABELS:
        outcome = await _seed_one(supabase, llm, label, execute=args.execute)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    logger.info("SUMMARY: %s", outcomes)
    if not args.execute:
        logger.info("dry-run — nothing written (pass --execute to apply)")
    else:
        logger.info(
            "NOTE: catalog ingestion starts once the payer-fallback release "
            "reaches prod (unsponsored targets defer Phase 1 on current prod code)."
        )


if __name__ == "__main__":
    asyncio.run(main())
