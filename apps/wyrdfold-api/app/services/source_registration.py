"""Register a company's ATS board as a pollable source from a pasted job URL.

The from-url target flow (a user creates a target from a JD URL) should also
*feed the job search*: if that URL is on a supported ATS, register the company's
board so the poller pulls MORE jobs from that company on every future cycle —
not just the single posting the user pasted. This is the second half of the
from-url gap (the first half, materializing the posting itself as a tailorable
job, lives in ``job_ingest``).

Sources are a global, shared, forever-polled corpus, so each registration is
attributed to the user and held to a per-user cap
(``settings.source_registration_cap_per_user``) inside the
``register_source_from_url`` SECURITY DEFINER RPC — a single account can't
script thousands of URL pastes into an unbounded source list. The ATS
classification + the dead-board filter (never poll a board with zero live
postings) happen here because they need a network probe; the cap + the ownership
ledger happen in the RPC because they must be server-authoritative.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from supabase import AsyncClient

from app.config import settings
from app.services.ats_detect import detect_ats, is_ats_url

logger = logging.getLogger(__name__)

# Bound the ATS probe so a slow/hanging board can't wedge the background task.
_DETECT_TIMEOUT_S = 20.0


async def _call_register_rpc(
    supabase: AsyncClient,
    *,
    user_id: str,
    provider: str,
    board_token: str,
    company_name: str,
) -> str:
    """Invoke the ``register_source_from_url`` RPC and normalize its return.

    postgrest wraps a scalar-returning function as ``resp.data == "<status>"`` or
    a single-row list depending on schema-cache state, so accept both shapes.
    """
    resp = await supabase.rpc(
        "register_source_from_url",
        {
            "p_user_id": user_id,
            "p_provider": provider,
            "p_board_token": board_token,
            "p_company_name": company_name,
            "p_cap": settings.source_registration_cap_per_user,
        },
    ).execute()
    data: Any = resp.data
    if isinstance(data, str):
        return data
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            val = first.get("register_source_from_url")
            if isinstance(val, str):
                return val
    logger.warning("register_source_from_url returned unexpected shape: %r", data)
    return "error"


async def register_source_from_url(supabase: AsyncClient, *, user_id: str, final_url: str) -> str:
    """Best-effort: if ``final_url`` is a supported, live ATS board, register it
    as a pollable source owned by ``user_id`` (capped). Returns an outcome string
    (for logging + tests) and NEVER raises — a registration failure must not
    affect the target the user just created.

    Outcomes: ``unclassified`` (not a pollable ATS — LinkedIn, a custom careers
    page, …), ``dead_board`` (classified but zero live postings), ``error`` (the
    probe or RPC failed), or one of the RPC statuses ``registered`` / ``linked``
    / ``already_owned`` / ``cap_reached``.
    """
    # Gate on a *recognized ATS host* before probing. detect_ats otherwise falls
    # back to guessing a slug from the domain stem and probing every provider, so
    # an arbitrary URL (linkedin.com/jobs/… → guessed slug "linkedin") can match
    # an unrelated board and register a wrong global source. Discovery never hits
    # this (its inputs are site-restricted); arbitrary user URLs do.
    if not is_ats_url(final_url):
        return "unclassified"

    try:
        async with asyncio.timeout(_DETECT_TIMEOUT_S):
            detect = await detect_ats(final_url)
    except Exception:
        logger.warning("source registration: detect_ats failed for %s", final_url, exc_info=True)
        return "error"

    if detect is None:
        return "unclassified"
    if detect.job_count == 0:
        # Classified but nothing to poll — registering it would just burn poll
        # cycles on a dead board. (Target-driven discovery applies the same filter.)
        return "dead_board"

    try:
        outcome = await _call_register_rpc(
            supabase,
            user_id=user_id,
            provider=detect.provider,
            board_token=detect.board_token,
            company_name=detect.company_name,
        )
    except Exception:
        logger.exception(
            "source registration RPC failed for %s (token=%s)", final_url, detect.board_token
        )
        return "error"

    logger.info(
        "source registration for user %s: %s (provider=%s token=%s jobs=%d)",
        user_id,
        outcome,
        detect.provider,
        detect.board_token,
        detect.job_count,
    )
    return outcome
