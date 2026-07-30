"""Retract stage-1 ``promising`` verdicts on hard off-family (job, target) pairs.

Phase-1 title triage runs at poll time, BEFORE the async qualification tagger
has stamped ``jobs.role_family`` — so it can admit pairs the #277 family gate
would reject (e.g. a ``customer_experience`` listing admitted for an
``engineering`` target on 2026-07-08). Stage-2 preserves the persisted
promising floor by design (#517), so those verdicts never self-heal: the
Mercury "Customer Support Specialist - Payroll" row was later graded to
score 0 and STILL carried ``promising = true``.

The read-time family gate hides these rows from /jobs, and (post
fix/offfamily-membership-gate) from the /search membership badge. This script
fixes the DATA so no future consumer of ``scores.promising`` trips over them:
``promising := false`` wherever the job's family and the target's family are
both known and different — exactly the pairs ``passes_family_gate`` rejects,
imported from the same module so the backfill can't drift from the gate.

``excluded`` is left untouched (it carries user-preference/logistics
semantics), as is ``phase1_confidence`` (an honest record of the original
triage call).

Idempotent: flipped rows leave the ``promising IS TRUE`` selection set.
Job family comes from the trigger-synced ``scores.job_role_family`` denorm
(verified 0-drift against ``jobs.role_family`` in prod, 2026-07-30).

Usage::

    cd apps/wyrdfold-api
    uv run python scripts/backfill_offfamily_promising.py            # dry-run
    uv run python scripts/backfill_offfamily_promising.py --execute

Env required: ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

from supabase import Client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.qualification.family_gate import passes_family_gate
from app.supabase_pool import get_supabase_pool, init_supabase

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_offfamily_promising")

PAGE_SIZE = 1000
# in_() URL-encodes ~36 bytes per UUID; keep chunks small (#57 lesson).
WRITE_CHUNK = 100
WRITE_SLEEP_S = 0.1


def _target_families(supabase: Client) -> dict[str, tuple[str, str | None]]:
    """target_id -> (label, role_family) for every target."""
    rows = cast(
        list[dict[str, Any]],
        supabase.table("targets").select("id, label, role_family").execute().data or [],
    )
    return {r["id"]: (r.get("label") or r["id"][:8], r.get("role_family")) for r in rows}


def _promising_rows(supabase: Client) -> list[dict[str, Any]]:
    """Every scores row currently carrying a positive stage-1 verdict."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = (
            supabase.table("scores")
            .select("id, target_id, job_role_family")
            .eq("promising", True)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        page = cast(list[dict[str, Any]], resp.data or [])
        out.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write promising=false. Default is a report-only dry-run.",
    )
    args = parser.parse_args()

    init_supabase()
    supabase = get_supabase_pool()
    if supabase is None:
        raise SystemExit(
            "ERROR: Supabase not configured — check SUPABASE_URL + "
            "SUPABASE_SERVICE_ROLE_KEY in apps/wyrdfold-api/.env"
        )

    targets = _target_families(supabase)
    rows = _promising_rows(supabase)
    logger.info("promising=true rows scanned: %d (targets: %d)", len(rows), len(targets))

    to_flip: list[str] = []
    pair_counts: Counter[tuple[str, str, str]] = Counter()
    for r in rows:
        label, tfam = targets.get(r["target_id"], (r["target_id"][:8], None))
        jfam = cast("str | None", r.get("job_role_family"))
        if passes_family_gate(tfam, jfam):
            continue
        to_flip.append(cast(str, r["id"]))
        pair_counts[(label, cast(str, tfam), cast(str, jfam))] += 1

    logger.info("hard off-family promising rows to retract: %d", len(to_flip))
    for (label, tfam, jfam), n in pair_counts.most_common():
        logger.info("  %-52s %-18s <- %-18s %5d", label[:52], tfam, jfam, n)

    if not args.execute:
        logger.info("dry-run — nothing written (pass --execute to apply)")
        return

    flipped = 0
    for i in range(0, len(to_flip), WRITE_CHUNK):
        chunk = to_flip[i : i + WRITE_CHUNK]
        supabase.table("scores").update({"promising": False}).in_("id", chunk).execute()
        flipped += len(chunk)
        logger.info("  flipped %d/%d", flipped, len(to_flip))
        await asyncio.sleep(WRITE_SLEEP_S)

    logger.info("DONE: promising=false on %d rows", flipped)


if __name__ == "__main__":
    asyncio.run(main())
