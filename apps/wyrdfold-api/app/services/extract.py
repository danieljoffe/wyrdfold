"""Job metadata extraction from URLs (#500).

Three-tier extraction cascade:
  1. JSON-LD structured data (gold standard)
  2. HTML meta/OG tags + heuristics (fallback)
  3. Firecrawl for JS-rendered pages (gated behind API key)
"""

import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.services.jsonld import (
    _extract_jobs,
    _format_salary,
    _get_location,
    _get_str,
)
from app.services.scoring import _TAGGY_RE, strip_html

logger = logging.getLogger(__name__)

MANUAL_SOURCE_ID = "00000000-0000-4000-a000-000000000001"

# The "manual" pseudo-source row that user-pasted jobs (POST /jobs/manual)
# are filed under. It satisfies the NOT-NULL job_postings.source_id FK
# without belonging to a real polled board. ``enabled`` is False so the
# poller skips it; ``poll_interval_minutes`` stays inside the table's
# 5..10080 CHECK. Kept here (rather than only in the seed migration) so the
# manual-add path can self-heal a missing row at request time. See
# supabase/migrations/*_seed_manual_source.sql.
MANUAL_SOURCE_ROW: dict[str, Any] = {
    "id": MANUAL_SOURCE_ID,
    "provider": "manual",
    "board_token": "__manual__",
    "company_name": "Manually Added",
    "enabled": False,
    "poll_interval_minutes": 10080,
    "consecutive_failures": 0,
}

# Patterns for finding job description content areas.
# Matches BEM (job__description), kebab (job-description), and plain (jobdescription).
_JOB_CONTENT_SELECTORS = [
    {"class_": re.compile(r"job[-_]*description", re.I)},
    {"class_": re.compile(r"job[-_]*details", re.I)},
    {"class_": re.compile(r"job[-_]*post[-_]*container", re.I)},
    {"class_": re.compile(r"responsibilities", re.I)},
    {"class_": re.compile(r"qualifications", re.I)},
    {"id": re.compile(r"job[-_]*description", re.I)},
    {"id": re.compile(r"job[-_]*details", re.I)},
]


class ExtractionResult(BaseModel):
    title: str | None = None
    company_name: str | None = None
    location: str | None = None
    description_html: str | None = None
    salary_text: str | None = None
    tier: str = "none"  # "jsonld" | "html_meta" | "firecrawl" | "none"
    warnings: list[str] = []


# ---- Salary extraction (v2 — #503) ------------------------------------------
#
# Grown from the REAL formats prod has matched (regression corpus in
# tests/test_extract.py): "$120,000-$275,000", "$200K-$400K" (en-dash variants
# too), "$77,600.00 to $176,000.00", "$19.00 - 21.00" (second amount bare),
# "$17.89 - $26.35 / Hour", "/per year" units — plus the #503 additions:
# £/€/CA$ currencies, a trailing ISO code ("$190,800 [em-dash] $267,100 USD"),
# and guarded single-bound forms ("up to $180,000", "from £60k").

# Currency symbol, optionally region-prefixed (US$/CA$/AU$/NZ$).
_CUR = r"(?:(?:US|CA|AU|NZ)?\$|£|€)"
# An amount: 1,234 / 1234.56 / 120k. First amount requires the symbol; the
# second may omit it ("$19.00 - 21.00" is a real prod format). NB the k-suffix
# allows NO preceding whitespace — a greedy `\s*` there would eat the space the
# optional trailing ISO code needs (`$267,100 USD` silently losing its `USD`).
_AMT = rf"{_CUR}\s*\d[\d,]*(?:\.\d+)?[kK]?"
_AMT_BARE = rf"(?:{_CUR})?\s*\d[\d,]*(?:\.\d+)?[kK]?"
# Range separator: dash family (spaces optional) or a spaced joining word.
_SEP = r"(?:\s*[-–—~]\s*|\s+(?:to|through)\s+)"
# Pay period, e.g. "/yr", " per hour", "/ Hour", "/per year".
_UNIT = r"(?:\s*/?\s*(?:yr|year|annually|per\s+year|per\s+annum|hr|hour|hourly|per\s+hour))?"
# Trailing ISO currency code, e.g. "$267,100 USD".
_ISO = r"(?:\s+(?:USD|CAD|AUD|NZD|GBP|EUR))?"

_SALARY_RANGE_RE = re.compile(rf"{_AMT}{_SEP}{_AMT_BARE}{_UNIT}{_ISO}", re.I)

# Single-bound forms need an explicit cue word — a bare "$180,000" anywhere in
# a JD is far too promiscuous (funding rounds, revenue, perk values).
_SALARY_BOUND_RE = re.compile(
    rf"(?:up\s+to|from|starting\s+(?:at|from)|minimum\s+of)\s+({_AMT}){_UNIT}{_ISO}",
    re.I,
)
# First numeric in a match, for the single-bound plausibility guard.
_FIRST_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")
_HOURLY_HINT_RE = re.compile(r"hr|hour", re.I)


def _plausible_single_bound(match: re.Match[str]) -> bool:
    """Reject perk-sized 'up to $500' hits: a single-bound salary must be
    ≥ $1k (or k-suffixed), unless an explicit hourly unit makes small
    amounts legitimate ('starting at $25/hour'). Ranges are NOT gated —
    real unit-less hourly ranges exist in prod ("$49 - $57")."""
    num = _FIRST_NUM_RE.search(match.group(1))
    if not num:
        return False
    if num.group(2):  # k-suffix
        return True
    value = float(num.group(1).replace(",", ""))
    if _HOURLY_HINT_RE.search(match.group(0)):
        return value >= 10
    return value >= 1_000


def extract_salary_from_text(text: str) -> str | None:
    """Best-effort salary extraction from plain text via regex.

    Ranges win over single-bound forms ("from $150k to $190k" must yield the
    whole range, not the "from" prefix). Trailing punctuation is trimmed —
    prod once stored "$148,000 - $185,000," verbatim.
    """
    m = _SALARY_RANGE_RE.search(text)
    if m is None:
        m = _SALARY_BOUND_RE.search(text)
        if m is None or not _plausible_single_bound(m):
            return None
    return m.group(0).strip().rstrip(",.;:")


# A leading escaped tag at ANY escape depth: "&lt;", "&amp;lt;",
# "&amp;amp;lt;", … — each extra escape round wraps the & as "&amp;".
_ESCAPED_DOC_PREFIX_RE = re.compile(r"^&(?:amp;)*lt;")


def unescape_html_doc(description_html: str | None) -> str | None:
    """Heal a stored JD whose ENTIRE payload was HTML-escaped at ingestion.

    Greenhouse's Job Board API delivered ``content`` escaped (#500 — fixed at
    ingestion in ``services/greenhouse.py``), so rows stored before that fix
    hold ``&lt;div ...`` verbatim. The poller only re-delivers content for
    rows that pass TODAY'S free gates on a polled, still-listed board, so the
    stored backlog never converges on its own — this is the column-level heal
    the backfill script applies.

    Returns the unescaped document, or ``None`` when the input doesn't look
    like a fully-escaped document (already-real markup, prose, empty) —
    callers must treat ``None`` as "leave the row alone". Bounded: at most 3
    unescape rounds (prod holds only single-escaped rows; deeper depths are
    handled defensively), and the result must start with a real tag shape or
    the heal is rejected — never corrupt a row on a false positive.
    """
    if not description_html or not _ESCAPED_DOC_PREFIX_RE.match(description_html):
        return None
    healed = description_html
    for _ in range(3):
        if not _ESCAPED_DOC_PREFIX_RE.match(healed):
            break
        healed = html.unescape(healed)
    if healed.startswith("<") and _TAGGY_RE.match(healed):
        return healed
    return None


def extract_salary_from_html(description_html: str | None) -> str | None:
    """Salary from a JD's HTML — the ONE entry point for every stored-HTML
    caller (poller, manual ingest, backfill, JSON-LD descriptions).

    Structural first: Greenhouse renders compensation in a dedicated
    ``pay-range`` block (``<span>$190,800</span><span class="divider">—</span>
    <span>$267,100 USD</span>``) — parsing it directly beats prose regex for
    the most common board AND guarantees the real comp wins over any earlier
    dollar figure in the body text. Falls back to the shared token stream
    (``strip_html``, escaped-HTML-defensive) + prose regex.
    """
    if not description_html:
        return None
    if "pay-range" in description_html:
        soup = BeautifulSoup(description_html, "html.parser")
        el = soup.select_one(".content-pay-transparency .pay-range") or soup.select_one(
            ".pay-range"
        )
        if el is not None:
            structural = extract_salary_from_text(" ".join(el.get_text(separator=" ").split()))
            if structural:
                return structural
    return extract_salary_from_text(strip_html(description_html))


def _company_from_domain(url: str) -> str:
    """Derive a company name from a URL's hostname.

    Examples: jobs.stripe.com → Stripe, careers.google.com → Google
    """
    hostname = urlparse(url).hostname or ""
    parts = hostname.lower().split(".")
    # Skip common prefixes
    skip = {"www", "jobs", "careers", "boards", "apply", "hire", "recruiting"}
    for part in parts:
        if part not in skip and len(part) > 1:
            return part.capitalize()
    # Fallback: second-level domain
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return hostname


def _extract_from_jsonld(html: str) -> ExtractionResult | None:
    """Tier 1: Extract job metadata from JSON-LD structured data."""
    postings = _extract_jobs(html)
    if not postings:
        return None

    posting = postings[0]
    title = _get_str(posting, "title") or _get_str(posting, "jobTitle")
    if not title:
        return None

    description = _get_str(posting, "description")
    location = _get_location(posting)

    company = None
    org = posting.get("hiringOrganization")
    if isinstance(org, dict):
        company = _get_str(org, "name")

    salary = _format_salary(posting)
    if not salary and description:
        # JSON-LD descriptions are frequently HTML — same html-aware entry.
        salary = extract_salary_from_html(description)

    return ExtractionResult(
        title=title,
        company_name=company or None,
        location=location,
        description_html=description or None,
        salary_text=salary,
        tier="jsonld",
    )


def _meta_content(soup: BeautifulSoup, prop: str) -> str | None:
    """Safely extract string content from a <meta property=...> tag."""
    tag = soup.find("meta", attrs={"property": prop})
    if tag:
        val = tag.get("content")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _meta_name_content(soup: BeautifulSoup, name: str) -> str | None:
    """Safely extract string content from a <meta name=...> tag."""
    tag = soup.find("meta", attrs={"name": name})
    if tag:
        val = tag.get("content")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_from_html_meta(html: str, url: str) -> ExtractionResult | None:
    """Tier 2: Extract job metadata from OG tags and HTML heuristics."""
    soup = BeautifulSoup(html, "html.parser")

    # Title: og:title → <title>
    title = _meta_content(soup, "og:title")
    if not title:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            title = title_tag.string.strip()

    if not title:
        return None

    # Company: og:site_name → domain
    company = _meta_content(soup, "og:site_name")
    if not company:
        company = _company_from_domain(url)

    # Description: content area → og:description → meta description
    description = None
    for selector in _JOB_CONTENT_SELECTORS:
        el = soup.find(**selector)  # type: ignore[call-overload]
        if el:
            description = str(el)
            break
    if not description:
        description = _meta_content(soup, "og:description")
    if not description:
        description = _meta_name_content(soup, "description")

    # Location: og:locale or leave None
    location: str | None = None
    locale_val = _meta_content(soup, "og:locale")
    if locale_val and locale_val != "en_US":
        location = locale_val

    return ExtractionResult(
        title=title,
        company_name=company or None,
        location=location,
        description_html=description or None,
        tier="html_meta",
    )


async def _extract_from_firecrawl(url: str) -> ExtractionResult:
    """Tier 3: Use Firecrawl API for JS-rendered pages (gated).

    Always returns ExtractionResult. On failure, tier="none" and warnings
    explain why so the caller can surface them to the user.
    """
    from app.config import settings

    if not settings.firecrawl_api_key:
        return ExtractionResult(tier="none", warnings=["firecrawl_unavailable"])

    try:
        from app.http_client import get_http_client

        client = get_http_client()
        resp = await client.post(
            "https://api.firecrawl.dev/v2/scrape",
            json={"url": url, "formats": ["html"]},
            headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            return ExtractionResult(
                tier="none", warnings=[f"firecrawl_failed:http_{resp.status_code}"]
            )

        data = resp.json().get("data", {})
        fc_html = data.get("html", "")
        if not fc_html:
            return ExtractionResult(tier="none", warnings=["firecrawl_failed:empty_html"])

        # Run tiers 1+2 on the Firecrawl-rendered HTML
        result = _extract_from_jsonld(fc_html) or _extract_from_html_meta(fc_html, url)
        if result:
            result.tier = "firecrawl"
            return result
        return ExtractionResult(tier="none", warnings=["firecrawl_failed:no_metadata"])

    except Exception:
        logger.exception("Firecrawl extraction failed for %s", url)
        return ExtractionResult(tier="none", warnings=["firecrawl_failed:exception"])


def extract_job_from_html(html: str, url: str) -> ExtractionResult:
    """Run the synchronous extraction tiers (1 + 2) on pre-fetched HTML.

    Tier 3 (Firecrawl) requires async and a separate fetch, so it is
    handled by the caller when tiers 1+2 fail.
    """
    # Tier 1: JSON-LD
    result = _extract_from_jsonld(html)
    if result:
        return result

    # Tier 2: HTML meta/OG heuristics
    result = _extract_from_html_meta(html, url)
    if result:
        return result

    # Nothing found
    return ExtractionResult(
        tier="none",
        warnings=["extraction_failed:no_metadata_found"],
    )
