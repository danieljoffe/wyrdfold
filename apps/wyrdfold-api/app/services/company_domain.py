"""#470: company-domain enrichment — the key logo services link by.

A "company" here is a free-text name copied from ``sources`` onto every
job row; logo services (Brandfetch, favicon endpoints) key on a company
DOMAIN, which is stored nowhere and not derivable from the ATS board URL
(the board slug is a lossy stem — ``linear56``, ``dbtlabs``). This module
closes that gap once per source row:

    candidate stems from company_name + board_token
        -> ``https://{stem}.com`` (then ``.io``) probed over an
           SSRF-safe client
        -> the first candidate that ANSWERS is stored on
           ``sources.domain``

Design constraints (docs/research-wyrdfold-company-logos.md):

- **Links only, ever** — nothing here fetches or stores an image; the
  client builds provider URLs from the stored domain.
- **Verification is "answers HTTP", and that is WEAK.** Any non-5xx
  response counts — 403, 404, a parking page, or an unrelated live site
  that happens to own the guessed stem. When the guess is wrong, the
  consequence is NOT "falls back to initials": if that wrong domain
  serves a valid favicon, the UI renders ANOTHER COMPANY'S LOGO, and
  only a domain whose logo requests all fail lands on the monogram.
  Measured example: ``Linear`` resolves to ``linear.io``; the real
  company is ``linear.app``.

  This is why the enrichment does not run itself. There is no scheduled
  tick and no source-creation hook — a human runs the backfill script
  after accepting that error rate (or after strengthening resolution
  with a name->domain lookup; see the research doc). A stored wrong
  domain is corrected by nulling the row and re-running.
- **SSRF posture**: candidate hostnames derive from board-published
  names/slugs — external input — so probes ride the IP-pinning transport
  (#192), same as url-health.
- Redirect hops stay WITHIN the probe; the STORED value is the candidate
  we asked about, not the redirect target. ``linear56.com`` redirecting
  to an unrelated parking lot must not launder the parking lot's domain
  into the catalog; a candidate that merely redirects ``example.com`` ->
  ``www.example.com`` still stores the clean apex.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, cast

import httpx
from supabase import AsyncClient

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = httpx.Timeout(6.0, connect=4.0)
_USER_AGENT = "wyrdfold-domain-enrichment/1.0 (+https://wyrdfold.com)"

# TLDs tried per stem, in order. ``.com`` first (overwhelmingly the common
# case for the companies on ATS boards), ``.io`` as the one startup-heavy
# alternate. Every extra TLD is another network probe per source — resist
# growing this list; a company we miss renders initials, not an error.
_CANDIDATE_TLDS = (".com", ".io")

# Sources that aren't a single company. The shared manual pseudo-source
# pools many employers under one row — any domain stored there would be
# wrong for most of its jobs.
_PSEUDO_SOURCE_NAMES = frozenset({"manually added"})

# A plausible bare label: letters/digits, len >= 3 (1-2 char stems like
# ``x`` collide with squatters far more often than they hit the company).
_STEM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def _stem_from_name(company_name: str) -> str | None:
    """``"dbt Labs"`` -> ``dbtlabs``; None when nothing plausible remains."""
    stem = re.sub(r"[^a-z0-9]", "", company_name.lower())
    return stem if len(stem) >= 3 and _STEM_RE.match(stem) else None


def _stem_from_slug(board_token: str) -> str | None:
    """``datadog81`` -> ``datadog`` (trailing digits are ATS-slug noise —
    ``datadog81.com`` is a squatter, ``datadog.com`` is the company);
    ``dbtlabs`` -> ``dbtlabs``. None when nothing plausible remains."""
    slug = board_token.strip().lower()
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"\d+$", "", slug)
    return slug if len(slug) >= 3 and _STEM_RE.match(slug) else None


def candidate_domains(company_name: str, board_token: str) -> list[str]:
    """Ordered, deduplicated candidate domains for one source row.

    Name-derived stems come first: the display name is what a human would
    type, the slug is what an ATS generated (and mangles more often).
    Pseudo-sources (the pooled manual row) get no candidates at all.
    """
    if company_name.strip().lower() in _PSEUDO_SOURCE_NAMES:
        return []
    stems: list[str] = []
    for stem in (_stem_from_name(company_name), _stem_from_slug(board_token)):
        if stem and stem not in stems:
            stems.append(stem)
    return [f"{stem}{tld}" for stem in stems for tld in _CANDIDATE_TLDS]


async def _answers_http(client: httpx.AsyncClient, domain: str) -> bool:
    """True when ``https://{domain}`` answers with any non-5xx status.

    4xx counts as answering — a site that 403s bots still exists and its
    logo endpoints usually work. Connection/TLS/DNS failures and 5xx do
    not. Never raises.
    """
    try:
        resp = await client.get(f"https://{domain}", follow_redirects=True)
    except Exception:
        return False
    return resp.status_code < 500


async def resolve_company_domain(
    company_name: str, board_token: str, *, client: httpx.AsyncClient | None = None
) -> str | None:
    """The first candidate domain that answers HTTP, or None.

    Probes sequentially in candidate order (first hit wins — ``.com``
    before ``.io``, name before slug) so the cheapest correct answer
    stops the network cost.
    """
    candidates = candidate_domains(company_name, board_token)
    if not candidates:
        return None

    async def _probe_all(c: httpx.AsyncClient) -> str | None:
        for domain in candidates:
            if await _answers_http(c, domain):
                return domain
        return None

    if client is not None:
        return await _probe_all(client)

    from app.services.safe_http import build_ssrf_safe_transport

    async with httpx.AsyncClient(
        transport=build_ssrf_safe_transport(),
        timeout=_PROBE_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
        max_redirects=5,
        verify=True,
    ) as owned:
        return await _probe_all(owned)


async def enrich_missing_source_domains(
    supabase: AsyncClient,
    *,
    limit: int = 50,
    concurrency: int = 4,
    after_id: str | None = None,
) -> tuple[int, int, str | None]:
    """Enrich up to ``limit`` sources whose ``domain`` is NULL; returns
    ``(examined, enriched, last_id)``.

    ``after_id`` is a keyset cursor over ``sources.id``: rows whose
    candidates all fail STAY NULL, so a caller that re-fetched the head of
    the NULL set each batch would re-probe the same misses forever —
    thread ``last_id`` back in instead, and restart from None on the next
    RUN to give yesterday's misses their cheap retry. Idempotent:
    enriched rows leave the NULL set; new sources are picked up by the
    next full run.
    """
    query = supabase.table("sources").select("id, company_name, board_token").is_("domain", "null")
    if after_id is not None:
        query = query.gt("id", after_id)
    resp = await query.order("id").limit(limit).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return 0, 0, None

    from app.services.safe_http import build_ssrf_safe_transport

    enriched = 0
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        transport=build_ssrf_safe_transport(),
        timeout=_PROBE_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
        max_redirects=5,
        verify=True,
    ) as client:

        async def _one(row: dict[str, Any]) -> None:
            nonlocal enriched
            async with sem:
                domain = await resolve_company_domain(
                    str(row.get("company_name") or ""),
                    str(row.get("board_token") or ""),
                    client=client,
                )
            if domain is None:
                return
            try:
                await (
                    supabase.table("sources")
                    .update({"domain": domain})
                    .eq("id", row["id"])
                    .is_("domain", "null")  # never clobber a concurrent write
                    .execute()
                )
                enriched += 1
            except Exception:
                logger.exception("domain enrichment write failed for source %s", row["id"])

        await asyncio.gather(*(_one(r) for r in rows))

    logger.info("domain enrichment: examined=%d enriched=%d", len(rows), enriched)
    return len(rows), enriched, str(rows[-1]["id"])
