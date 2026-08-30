"""Re-detect a failing board's ATS before the failure backoff disables it (#912).

A board answering 404 is evidence the board TOKEN is stale, not that the
company stopped hiring. Companies rename their board or migrate ATS and the
identifier we hold stops resolving; treating the identifier as though it
described the entity is the bug. Measured on the 139 enabled sources that were
failing on 2026-08-29: 27 of them resolved to a DIFFERENT live board under
their own company name, and every one of the top hits was a company obviously
still hiring (Notion, Ramp, Plaid, Sentry, Supabase — all Greenhouse -> Ashby).

This module answers one question for one source, and writes nothing:

    is this board dead, moved, or merely flaking?

:func:`redetect_source` returns a verdict; :mod:`app.services.poller` decides
what to persist. Four outcomes:

``still_live``
    The token we hold answers right now. The failed poll was transient and
    disabling would be simply wrong. Measured: 33 of the 139 — all Workday,
    all with ``last_error`` = "returned 200 with a non-JSON body" (Workday
    intermittently serves an interstitial instead of JSON), and replaying the
    real ``fetch_workday_jobs`` against a sample of them succeeded on every
    one. These are not dead boards.
``repoint``
    A different provider/token is live under this company, carries at least
    one posting, and no other source owns that token. The caller re-points the
    EXISTING row rather than registering a new one: ``jobs.source_id`` is an
    FK, so a new row would orphan the company's existing listings.
``collision``
    We found a live board but another ``sources`` row already holds that
    ``board_token``. ``sources`` carries ``UNIQUE (board_token)`` (verified
    live in prod, ``job_sources_board_token_key``), so re-pointing would raise
    23505 — and even without the constraint it would duplicate a company.
    Measured: 5 of the 139.
``not_found``
    Nothing resolved. The caller disables exactly as it does today.

Deliberately NOT done here: archiving listings, touching the ``url_health``
queue, or any company-wide archival. #912 steps 2 and 3 are separate work.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.services.ats_detect import _PROBERS, clean_company_name, detect_ats, probe_board
from app.services.db_write import poll_db_read

logger = logging.getLogger(__name__)

# Providers whose ``board_token`` this module knows how to reason about: the
# four slug-based ATSs plus Workday's composite token. Prod also holds one
# ``manual`` source whose token is the literal string "manual" — feeding that
# to the slug ladder would probe four providers for a board called "manual"
# and re-point a hand-curated row onto whatever answered. A token we cannot
# interpret is not a token we may guess from.
_REDETECTABLE_PROVIDERS = frozenset({*_PROBERS, "workday"})

# Ceiling on the WHOLE re-detection for one source, probes included. This runs
# inside the poll cycle, and the ladder below can issue up to 13 requests
# (1 current-board probe + 3 candidate slugs x 4 providers). At the shared
# client's 15 s read timeout that is minutes of wall clock, which would wedge
# the cycle. ``source_registration`` bounds its single detect at 20 s for the
# same reason; this budget is larger because the ladder is longer, and a source
# that exhausts it just falls through to today's disable.
REDETECT_TIMEOUT_S = 30.0

# Non-slug runs collapse to a single hyphen: "Black Forest Labs" ->
# "black-forest-labs".
_NON_SLUG_RUN = re.compile(r"[^a-z0-9]+")

# Slug floor. One character is not an identifier, it is a coin flip against
# every board on four providers.
_MIN_SLUG_LEN = 2

RedetectAction = Literal["still_live", "repoint", "collision", "not_found"]


@dataclass(frozen=True)
class RedetectOutcome:
    """Verdict for one failing source. Carries no side effects."""

    action: RedetectAction
    provider: str | None = None
    board_token: str | None = None
    company_name: str | None = None
    job_count: int = 0
    # For ``collision``: the id of the source that already owns the token.
    blocked_by: str | None = None


def _plain_slug(name: str) -> str:
    """The form :func:`detect_ats` derives internally from a company name."""
    return re.sub(r"[^a-z0-9._-]", "", name.lower().replace(" ", ""))


def _hyphen_slug(name: str) -> str:
    return _NON_SLUG_RUN.sub("-", name.lower()).strip("-")


def slug_candidates(source: dict[str, Any]) -> list[str]:
    """Detection inputs to try, in order, most-trustworthy first.

    ``company_name`` is messy by construction: boards let a company title
    theirs "Acme Careers page" and that title becomes our ``company_name``
    (see :func:`ats_detect.clean_company_name`). We route the name through the
    same cleaner before deriving a slug — a no-op for all 139 of the currently
    failing sources, since names registered through ``detect_ats`` are already
    cleaned, but the corpus also holds seeded and discovery-registered rows and
    the cleaning costs nothing.

    Three rungs, each measured against the failing cohort before being
    included; the ladder stops at the first hit, so the later rungs only ever
    cost requests for a source the earlier ones missed:

    1. **The company name.** 27 recoveries. Straight to ``detect_ats``, which
       does its own sluggification.
    2. **The board token we already hold** (the Workday tenant for a composite
       Workday token). +3: a stale token is often a better slug than a board
       title — "Thinking Machines Lab" sluggifies to ``thinkingmachineslab``
       but the board is ``thinkingmachines``, which is what we already held.
    3. **The hyphenated company name**, when it differs from rung 1. +2
       ("Black Forest Labs" -> ``black-forest-labs``). Only 22 of the 70
       misses even produce a distinct candidate here.

    Deduplicated on the DERIVED slug, not the raw input: for a one-word name
    like "Notion" rungs 1 and 3 collapse to the same probe, and paying for it
    twice would double the request count for no new information.
    """
    candidates: list[str] = []

    def _add(value: str | None) -> None:
        if value and len(value) >= _MIN_SLUG_LEN and value not in candidates:
            candidates.append(value)

    name = clean_company_name(str(source.get("company_name") or "").strip())
    _add(_plain_slug(name))

    token = str(source.get("board_token") or "").strip()
    if source.get("provider") == "workday":
        # ``{base_url}|{tenant}|{site}`` — only the tenant is a usable slug.
        parts = token.split("|")
        _add(parts[1].lower() if len(parts) == 3 else None)
    else:
        _add(token.lower())

    _add(_hyphen_slug(name))
    return candidates


async def _token_owner(supabase: Any, board_token: str, *, exclude_id: str) -> str | None:
    """Id of another ``sources`` row already holding ``board_token``, if any.

    Checked against EVERY row, not just enabled ones: the uniqueness constraint
    does not care about ``enabled``, so a disabled duplicate blocks the write
    just as hard as a live one.
    """
    resp = await poll_db_read(
        supabase,
        # limit(2) not (1): with UNIQUE(board_token) one row is all there can
        # be, but if that ever changes a self-match must not hide another owner.
        lambda c: c.table("sources").select("id").eq("board_token", board_token).limit(2),
        label="poll redetect token-owner",
    )
    for row in resp.data or []:
        row_id = str(row.get("id") or "")
        if row_id and row_id != exclude_id:
            return row_id
    return None


async def redetect_source(
    supabase: Any, source: dict[str, Any], *, timeout_s: float = REDETECT_TIMEOUT_S
) -> RedetectOutcome:
    """Decide what the failing ``source`` really is. Never raises.

    Any failure inside the probe ladder degrades to ``not_found``, i.e. exactly
    today's behaviour (disable): a re-detection that cannot run must never be
    the reason a source escapes the backoff.
    """
    source_id = str(source.get("id") or "")
    provider = str(source.get("provider") or "")
    board_token = str(source.get("board_token") or "")
    current = (provider, board_token)

    if provider not in _REDETECTABLE_PROVIDERS:
        return RedetectOutcome(action="not_found")

    found = None
    try:
        async with asyncio.timeout(timeout_s):
            # 1. Is the board we hold actually dead? One request, and it is the
            #    question the backoff got wrong: a transient failure looks
            #    identical to a stale token from the poller's side.
            live = await probe_board(provider, board_token)
            if live is not None:
                return RedetectOutcome(
                    action="still_live",
                    provider=provider,
                    board_token=board_token,
                    company_name=live.company_name,
                    job_count=live.job_count,
                )

            # 2. It is dead. Has the company moved?
            for slug in slug_candidates(source):
                detected = await detect_ats(slug)
                if detected is None:
                    continue
                if (detected.provider, detected.board_token) == current:
                    # The direct probe said dead and the ladder says live —
                    # trust the positive and leave the source alone.
                    return RedetectOutcome(
                        action="still_live",
                        provider=provider,
                        board_token=board_token,
                        company_name=detected.company_name,
                        job_count=detected.job_count,
                    )
                if detected.job_count <= 0:
                    # A live board with zero postings is not enough evidence to
                    # move a company onto it — the same bar ``source_registration``
                    # applies when it refuses a ``dead_board``. Keep looking.
                    continue
                found = detected
                break
    except Exception:
        logger.warning(
            "re-detect probe failed for source %s (%s/%s)",
            source_id,
            provider,
            board_token,
            exc_info=True,
        )
        return RedetectOutcome(action="not_found")

    if found is None:
        return RedetectOutcome(action="not_found")

    try:
        owner = await _token_owner(supabase, found.board_token, exclude_id=source_id)
    except Exception:
        # Without a clean answer we cannot know the write is safe, and a 23505
        # on a UNIQUE(board_token) violation is a worse outcome than one more
        # cycle of the status quo.
        logger.warning(
            "re-detect could not check token ownership for %s; not re-pointing %s",
            found.board_token,
            source_id,
            exc_info=True,
        )
        return RedetectOutcome(action="not_found")

    if owner is not None:
        return RedetectOutcome(
            action="collision",
            provider=found.provider,
            board_token=found.board_token,
            company_name=found.company_name,
            job_count=found.job_count,
            blocked_by=owner,
        )

    return RedetectOutcome(
        action="repoint",
        provider=found.provider,
        board_token=found.board_token,
        company_name=found.company_name,
        job_count=found.job_count,
    )
