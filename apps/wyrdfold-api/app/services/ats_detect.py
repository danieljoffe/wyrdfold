import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.http_client import get_http_client
from app.services.ashby import ASHBY_BASE
from app.services.greenhouse import GREENHOUSE_BASE
from app.services.lever import LEVER_BASE
from app.services.smartrecruiters import SMARTRECRUITERS_BASE

PROBE_DELAY = 0.1

# Board titles double as our company_name, and providers let companies name
# their board "Acme Careers page" — which then labels every posting's Company
# column (prod: "ATOMS Careers page"; ux-sweep 2026-08-12 §D4). Strip the
# board-noise suffix once, at detection time. Conservative: only the exact
# trailing phrases, case-insensitive; a cleaning that would empty the name
# is discarded.
_BOARD_NOISE_SUFFIX = re.compile(
    r"\s+(?:careers?(?:\s+page|\s+site)?|job\s+board|jobs)\s*$",
    re.IGNORECASE,
)


def clean_company_name(name: str) -> str:
    cleaned = _BOARD_NOISE_SUFFIX.sub("", name).strip()
    return cleaned or name


@dataclass
class DetectResult:
    provider: str
    board_token: str
    company_name: str
    job_count: int

    def __post_init__(self) -> None:
        # Every provider path constructs a DetectResult, so normalizing here
        # covers greenhouse/lever/ashby/workday/smartrecruiters and any
        # future provider without per-site cleaning calls.
        self.company_name = clean_company_name(self.company_name)


# URL patterns that let us skip probing and go straight to a provider.
_URL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"boards\.greenhouse\.io/([a-z0-9][a-z0-9-]+)", re.I), "greenhouse"),
    (re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9][a-z0-9-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9][a-z0-9-]+)", re.I), "lever"),
    (re.compile(r"api\.lever\.co/v[01]/postings/([a-z0-9][a-z0-9-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9._-]+)", re.I), "ashby"),
    (re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9][a-z0-9._-]+)", re.I), "ashby"),
    (re.compile(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", re.I), "workday"),
    (
        re.compile(r"api\.smartrecruiters\.com/v1/companies/([a-zA-Z0-9-]+)", re.I),
        "smartrecruiters",
    ),
]

# Slug must be URL-safe, lowercase, 2-80 chars
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")

# Workday host: ``{tenant}.wd{n}.myworkdayjobs.com``. The tenant alone isn't
# enough to poll — ``fetch_workday_jobs`` needs the full
# ``{base_url}|{tenant}|{site}`` token, and the site only appears in the URL
# path. We therefore parse Workday URLs separately instead of routing them
# through the slug-based probers.
_WORKDAY_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$", re.I)

# Leading path segment that's a locale ("en-US", "fr-FR", "de"), not the
# career-site name.
_WORKDAY_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")


def _parse_workday_url(raw: str) -> tuple[str, str, str] | None:
    """Extract ``(base_url, tenant, site)`` from a myworkdayjobs.com URL.

    Returns None when the URL isn't a Workday host or carries no site
    segment (the bare tenant root is unpollable — see comment on
    ``_WORKDAY_HOST_RE``).
    """
    if "myworkdayjobs.com" not in raw.lower():
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw.lstrip('/')}")
    host = (parsed.hostname or "").lower()
    m = _WORKDAY_HOST_RE.match(host)
    if not m:
        return None
    segments = [s for s in (parsed.path or "").split("/") if s]
    if segments and _WORKDAY_LOCALE_RE.match(segments[0]):
        segments = segments[1:]
    if not segments:
        return None
    return f"https://{host}", m.group("tenant").lower(), segments[0]


async def _probe_workday(
    base_url: str, tenant: str, site: str, client: httpx.AsyncClient
) -> DetectResult | None:
    """Probe Workday's CXS list endpoint for the board's total job count."""
    url = f"{base_url}/wday/cxs/{tenant}/{site}/jobs"
    try:
        resp = await client.post(
            url,
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    total = data.get("total")
    if not isinstance(total, int):
        return None
    return DetectResult(
        provider="workday",
        board_token=f"{base_url}|{tenant}|{site}",
        company_name=tenant.replace("-", " ").title(),
        job_count=total,
    )


def _parse_input(raw: str) -> tuple[str | None, str]:
    """Parse user input into (provider_hint, slug).

    Returns (provider, slug) where provider is None if we need to probe all.
    """
    raw = raw.strip()

    # Try matching known ATS URL patterns
    for pattern, provider in _URL_PATTERNS:
        m = pattern.search(raw)
        if m:
            return (provider, m.group(1).lower())

    # If it looks like a URL, extract the domain stem as slug
    if "://" in raw or raw.startswith("www."):
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.hostname or ""
        # Strip www. and TLD → "stripe.com" → "stripe"
        stem = host.removeprefix("www.").split(".")[0].lower()
        if stem and _SLUG_RE.match(stem):
            return (None, stem)

    # Treat as a plain slug / company name
    slug = re.sub(r"[^a-z0-9._-]", "", raw.lower().replace(" ", ""))
    if slug and _SLUG_RE.match(slug):
        return (None, slug)

    return (None, raw.lower().strip())


def is_ats_url(raw: str) -> bool:
    """True iff ``raw`` is a URL on a *recognized ATS host* (matches a known ATS
    URL pattern in ``_URL_PATTERNS``).

    This distinguishes a real board URL (``jobs.ashbyhq.com/acme/…``) from an
    arbitrary URL whose domain stem :func:`_parse_input` would otherwise fall
    back to guessing as a slug — which :func:`detect_ats` then probes against
    every provider. That guess path is fine for discovery (its inputs are Brave
    results already site-restricted to ATS hosts) and for company-name lookups,
    but WRONG for arbitrary user-pasted URLs: e.g. ``linkedin.com/jobs/view/…``
    guesses the slug ``linkedin`` and coincidentally matches a Greenhouse board,
    so from-url registration must gate on this first or it would register
    unrelated global sources from non-ATS URLs.
    """
    return any(pattern.search(raw) for pattern, _ in _URL_PATTERNS)


async def _probe_greenhouse(slug: str, client: httpx.AsyncClient) -> DetectResult | None:
    # Probe the jobs list, not the board root. The root endpoint
    # (``/v1/boards/{slug}``) returns only ``{name, content}`` — the old
    # ``len(data.get("departments", []))`` count was always 0, which made
    # source discovery filter every Greenhouse board as a dead board.
    url = f"{GREENHOUSE_BASE}/{slug}/jobs"
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        # ValueError covers a 200 with a non-JSON body (rate-limit HTML,
        # Cloudflare interstitial, a marketing page sharing the host).
        return None
    if not isinstance(data, dict):
        return None
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return None

    # The display name lives on the board root; fetch it best-effort and
    # fall back to the slug if it's unavailable.
    company_name = slug
    try:
        meta_resp = await client.get(f"{GREENHOUSE_BASE}/{slug}")
        if meta_resp.status_code == 200:
            meta = meta_resp.json()
            if isinstance(meta, dict):
                company_name = meta.get("name") or slug
    except (httpx.HTTPError, ValueError):
        pass

    return DetectResult(
        provider="greenhouse",
        board_token=slug,
        company_name=company_name,
        job_count=len(jobs),
    )


async def _probe_lever(slug: str, client: httpx.AsyncClient) -> DetectResult | None:
    url = f"{LEVER_BASE}/{slug}?mode=json&limit=1"
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, list) or len(data) == 0:
        return None
    # Lever doesn't expose board-level company name; use slug title-cased
    return DetectResult(
        provider="lever",
        board_token=slug,
        company_name=slug.replace("-", " ").title(),
        job_count=len(data),
    )


async def _probe_ashby(slug: str, client: httpx.AsyncClient) -> DetectResult | None:
    url = f"{ASHBY_BASE}/{slug}"
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        return None
    return DetectResult(
        provider="ashby",
        board_token=slug,
        company_name=data.get("organizationName", slug),
        job_count=len(jobs),
    )


async def _probe_smartrecruiters(slug: str, client: httpx.AsyncClient) -> DetectResult | None:
    url = f"{SMARTRECRUITERS_BASE}/{slug}/postings?limit=1"
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    content = data.get("content", [])
    if not isinstance(content, list) or len(content) == 0:
        return None
    total = data.get("totalFound", len(content))
    return DetectResult(
        provider="smartrecruiters",
        board_token=slug,
        company_name=slug.replace("-", " ").title(),
        job_count=total,
    )


_PROBERS = {
    "greenhouse": _probe_greenhouse,
    "lever": _probe_lever,
    "ashby": _probe_ashby,
    "smartrecruiters": _probe_smartrecruiters,
}

_PROBE_ORDER = ["greenhouse", "lever", "ashby", "smartrecruiters"]


async def probe_board(provider: str, board_token: str) -> DetectResult | None:
    """Probe ONE already-known ``(provider, board_token)`` pair directly.

    The inverse question to :func:`detect_ats`. ``detect_ats`` guesses a slug
    and asks "which provider hosts this company?"; this asks "is the board we
    already hold still serving?" — one request, no slug guessing, no
    cross-provider fan-out.

    Returns the ``DetectResult`` when the board answers with a listing, None
    when it doesn't (or when ``provider`` isn't one we can probe). A Workday
    ``board_token`` is the composite ``{base_url}|{tenant}|{site}`` the
    fetchers use; anything else is a plain slug.
    """
    client = get_http_client()
    if provider == "workday":
        parts = board_token.split("|")
        if len(parts) != 3:
            return None
        return await _probe_workday(parts[0], parts[1], parts[2], client)
    prober = _PROBERS.get(provider)
    if prober is None:
        return None
    return await prober(board_token, client)


async def detect_ats(raw_input: str) -> DetectResult | None:
    """Parse input (URL or company name), probe ATS providers, return first match."""
    client = get_http_client()

    # Workday URLs carry the site in the path, which the slug-based probers
    # can't represent — handle them before the generic parse. Previously
    # every Workday hit from discovery fell through to the other four
    # probers and came back unclassified.
    workday_parts = _parse_workday_url(raw_input)
    if workday_parts is not None:
        return await _probe_workday(*workday_parts, client)

    provider_hint, slug = _parse_input(raw_input)

    if not slug:
        return None

    if provider_hint == "workday":
        # Workday URL without a site segment — unpollable, and probing the
        # tenant slug against the other ATSs would just waste four requests.
        return None

    # If we know the provider from the URL, just probe that one
    if provider_hint and provider_hint in _PROBERS:
        return await _PROBERS[provider_hint](slug, client)

    # Otherwise probe all sequentially with a small delay
    for provider in _PROBE_ORDER:
        result = await _PROBERS[provider](slug, client)
        if result:
            return result
        await asyncio.sleep(PROBE_DELAY)

    return None
