"""Materialize a single URL-sourced job posting and score it against targets.

Shared by ``POST /jobs/manual`` (a user pastes a job URL → scored against the
caller's active targets) and the from-url target flow (a user creates a target
from a JD URL → the same posting is materialized and scored against the
just-created target). Both need the identical sequence — upsert a manual-source
``jobs`` row keyed by the URL, score it against a set of targets, recompute its
global score, and force-include it so a deliberately-added posting is never
buried by the negative-keyword pass — so it lives here once.

Materializing the posting is what makes it a first-class job: a ``scores`` row
under a target the user follows is what puts it in the ``/jobs`` list and gives
the résumé/cover tailor a ``job_posting_id`` to key on. The from-url flow used
to dissolve the posting into the shared target profile (a reference JD) and
never create a job, so it was never tailorable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from typing import Any, cast

from supabase import Client

from app.cache import job_list_cache
from app.models.targets import JobTarget
from app.services.extract import (
    MANUAL_SOURCE_ID,
    MANUAL_SOURCE_ROW,
    extract_salary_from_html,
    salary_columns,
)
from app.services.jd_parser import parse_jd
from app.services.location_parse import parse_location
from app.services.sanitize import sanitize_html
from app.services.target_scoring import score_and_upsert

logger = logging.getLogger(__name__)


def job_external_id(final_url: str) -> str:
    """The deterministic, numeric ``external_id`` a URL maps to.

    A bigint column, so hash the URL and take the low bits. Deterministic per
    URL, which is what makes the ``(source_id, external_id)`` upsert a natural
    dedup key: the same posting re-added (or added by another user) updates the
    one row instead of duplicating it.
    """
    return str(int(hashlib.sha256(final_url.encode()).hexdigest()[:15], 16))


def ensure_manual_source(supabase: Client) -> None:
    """Idempotently ensure the ``manual`` pseudo-source row exists.

    Jobs added by URL are filed under a fixed pseudo-source (``MANUAL_SOURCE_ID``)
    to satisfy the NOT-NULL ``jobs.source_id`` FK. A seed migration creates it;
    this self-heals if it's ever missing (fresh DB, manual wipe). ``on_conflict=
    "id"`` + ignore_duplicates makes it a no-op in the common case.
    """
    supabase.table("sources").upsert(
        MANUAL_SOURCE_ROW, on_conflict="id", ignore_duplicates=True
    ).execute()


async def materialize_and_score_job(
    supabase: Client,
    *,
    final_url: str,
    title: str | None,
    company_name: str | None,
    location: str | None,
    description_html: str | None,
    salary_text: str | None,
    targets: Sequence[JobTarget],
    set_included: bool = True,
) -> str | None:
    """Upsert the posting, score it against ``targets``, recompute its global
    score, and (when ``set_included``) force-include it under those targets.

    Returns the ``jobs.id`` (None only if the upsert returned no row). Scoring +
    force-include are skipped when there is no ``title`` or no ``targets`` (the
    posting still materializes so it can be scored later). All writes go through
    the service-role ``supabase`` client and the gated ``score_and_upsert`` /
    ``user_set_scores_included`` RPCs (SEC-H2). Every blocking round-trip is
    off-loaded to the threadpool (#107).
    """
    salary = salary_text
    if not salary and description_html:
        salary = extract_salary_from_html(description_html)

    loc = parse_location(location)
    row: dict[str, Any] = {
        "external_id": job_external_id(final_url),
        "source_id": MANUAL_SOURCE_ID,
        "title": title,
        "company_name": company_name,
        "location": location,
        "city": loc.city,
        "state": loc.state,
        "country": loc.country,
        "location_remote": loc.remote,
        "department": None,
        "description_html": sanitize_html(description_html) if description_html else "",
        "absolute_url": final_url,
        "score": 0,
        "score_breakdown": {},
        # No provider date for a manual add — NULL, so "Posted" falls back to
        # cataloged_at instead of masquerading as posted-today (R2).
        "source_posted_at": None,
        "salary_text": salary,
        **salary_columns(salary),
    }

    def _persist() -> Any:
        ensure_manual_source(supabase)
        return supabase.table("jobs").upsert(row, on_conflict="source_id,external_id").execute()

    resp = await asyncio.to_thread(_persist)
    posting_id: str | None = None
    if resp.data:
        posting_id = cast(dict[str, Any], resp.data[0]).get("id")

    if not (posting_id and title and targets):
        job_list_cache.invalidate()
        return posting_id

    # Stage-2 score against each target. Each call is independent + IO-bound
    # (sync SDK), so gather them off the loop rather than round-trip serially.
    desc = description_html or ""
    parsed = parse_jd(desc)
    results = await asyncio.gather(
        *[
            asyncio.to_thread(
                score_and_upsert,
                supabase,
                job_posting_id=posting_id,
                title=title,
                description_html=desc,
                target=t,
                parsed_jd=parsed,
                gated=True,
            )
            for t in targets
        ],
        return_exceptions=True,
    )
    for t, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            logger.error(
                "Target scoring failed for job %s target %s", posting_id, t.id, exc_info=result
            )
    # Force-include: a deliberately-added posting must be visible even when the
    # negative-keyword pass flagged ``excluded`` (e.g. a JD mentioning "mentor
    # junior engineers"). Scoped to the targets scored here so it never flips
    # rows under another user's targets (audit #24 F4). The score breakdown
    # keeps the penalty, so the badge colour still tells the truth.
    if set_included:
        target_ids = [t.id for t in targets]
        try:
            await asyncio.to_thread(
                lambda: supabase.rpc(
                    "user_set_scores_included",
                    {"p_job_posting_id": posting_id, "p_target_ids": target_ids},
                ).execute()
            )
        except Exception:
            logger.exception("Force-include update failed for job %s", posting_id)

    job_list_cache.invalidate()
    return posting_id
