import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from app.http_client import FetchExhaustedError, json_or_none, request_with_retry
from app.services.standard_job import StandardJob

logger = logging.getLogger(__name__)

MAX_JOBS = 200
PAGE_SIZE = 20
# Cap on per-posting detail fetches in flight so a 200-posting board doesn't
# slam Workday's CXS endpoint. Five mirrors the SmartRecruiters fan-out.
_DETAIL_CONCURRENCY = 5

# Cap on requests in flight to ONE Workday POD, across every tenant the
# poller is working concurrently (#646).
#
# Workday rate-limits at the pod edge, not per tenant: tenants share hosts
# (``<tenant>.wd5.myworkdayjobs.com``), and the enabled catalog is heavily
# pod-concentrated (wd1 373 tenants, wd5 252, wd3 149 of 903). The poller
# runs POLL_CONCURRENCY sources at once and each Workday source fans out
# ``_DETAIL_CONCURRENCY`` detail fetches, so several same-pod tenants
# routinely stacked 15-20 requests on one host — producing the observed
# 429 storms (whole bursts of cisco/msd/usfca/zillow 429ing in the same
# second). The per-source cap can't see that: it's per fetch call.
#
# The cost was silent: a detail fetch that exhausts its retries DROPS the
# posting (see the gather loop below), so 429 storms quietly under-ingest
# Workday boards. Throttling to the pod's real budget also RECLAIMS time —
# a 429'd request burns three attempts with backoff sleeps before failing.
_POD_CONCURRENCY = 4
# Keyed by (event loop id, pod host): an asyncio.Semaphore binds to the loop
# that first awaits it, so a module-global one would raise across loops
# (every pytest-asyncio test gets a fresh loop). Keying on the running loop
# keeps one real gate per pod per loop.
_pod_gates: dict[tuple[int, str], asyncio.Semaphore] = {}


def _pod_host(base_url: str) -> str:
    """``https://cisco.wd5.myworkdayjobs.com`` → ``wd5.myworkdayjobs.com``.

    Strips the tenant label so every tenant on a pod shares one gate. A
    host with no tenant prefix (or an unparseable base) gates on itself —
    conservative, never wider than the real host.
    """
    host = urlsplit(base_url).netloc.lower()
    if not host:
        return base_url.lower()
    parts = host.split(".")
    return ".".join(parts[1:]) if len(parts) > 2 else host


def _pod_gate(base_url: str) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), _pod_host(base_url))
    gate = _pod_gates.get(key)
    if gate is None:
        gate = asyncio.Semaphore(_POD_CONCURRENCY)
        _pod_gates[key] = gate
    return gate


async def _fetch_one_posting_detail(
    *, base_url: str, tenant: str, site: str, external_path: str
) -> dict[str, Any] | None:
    """GET the per-posting detail endpoint.

    Workday's CXS detail URL is the same base + cxs prefix + site + the
    externalPath returned by the list endpoint, e.g.
    ``https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/job/Japan---Tokyo/Senior-Manager--Sales_JR343895``.
    The response carries ``jobPostingInfo.jobDescription`` (the JD body
    we want) and ``jobPostingInfo.externalUrl`` (the human-facing apply
    page).
    """
    url = f"{base_url}/wday/cxs/{tenant}/{site}{external_path}"
    try:
        async with _pod_gate(base_url):
            resp = await request_with_retry("GET", url)
    except FetchExhaustedError as exc:
        logger.warning(
            "workday detail fetch exhausted retries for %s%s: %s",
            base_url,
            external_path,
            exc,
        )
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.warning(
            "workday detail %s returned %d for %s",
            base_url,
            resp.status_code,
            external_path,
        )
        return None
    body = json_or_none(resp, source=f"workday detail {url}")
    if body is None:
        return None
    if not isinstance(body, dict):
        return None
    info = body.get("jobPostingInfo")
    return info if isinstance(info, dict) else None


async def fetch_workday_jobs(board_token: str) -> list[StandardJob]:
    """Fetch jobs from Workday's internal CXS API.

    board_token format: "{base_url}|{tenant}|{site}"
    e.g. "https://salesforce.wd12.myworkdayjobs.com|salesforce|External_Career_Site"

    Two-phase fetch: paginated list endpoint surfaces titles + externalPaths,
    then per-posting detail endpoint returns the JD body and the human-facing
    apply URL. The list response's ``descriptionTeaser`` field is empty on
    every Workday board I've probed; the body lives exclusively on the
    detail endpoint. Without this we ship rows with ``description_html = ""``
    and the LLM analyzer 422s with "no description to analyze".

    Per-posting detail fetches fan out under ``_DETAIL_CONCURRENCY``. A
    posting whose detail fetch fails is dropped from this run rather than
    written with an empty body; the next poll cycle retries.
    """
    parts = board_token.split("|")
    if len(parts) != 3:
        return []

    base_url, tenant, site = parts
    list_url = f"{base_url}/wday/cxs/{tenant}/{site}/jobs"

    # Phase 1: paginated list pull. Collect (externalPath, list_item) so we
    # can pair list-time metadata (locationsText, postedOn) with detail-time
    # body when we fan out.
    shallow: list[dict[str, Any]] = []
    offset = 0
    while offset < MAX_JOBS:
        try:
            # Same pod gate as the detail fan-out: the list POSTs 429'd too
            # (becu / asurion / markelcorp / mpc / icf in the observed storm),
            # and a list failure loses the WHOLE board for the cycle.
            async with _pod_gate(base_url):
                resp = await request_with_retry(
                    "POST",
                    list_url,
                    json={
                        "appliedFacets": {},
                        "limit": PAGE_SIZE,
                        "offset": offset,
                        "searchText": "",
                    },
                )
        except FetchExhaustedError as exc:
            logger.warning(
                "workday list fetch exhausted retries for %s (offset %d): %s",
                board_token,
                offset,
                exc,
            )
            return []

        if resp.status_code != 200:
            logger.warning(
                "workday %s returned %d at offset %d",
                board_token,
                resp.status_code,
                offset,
            )
            return []

        data = json_or_none(resp, source=f"workday {board_token}")
        if data is None:
            return []
        postings = data.get("jobPostings", [])
        if not postings:
            break
        shallow.extend(postings)

        total = data.get("total", 0)
        offset += PAGE_SIZE
        if offset >= total:
            break

    if not shallow:
        return []

    # Phase 2: fan out detail fetches under the concurrency cap.
    semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)

    async def _bounded_detail(external_path: str) -> dict[str, Any] | None:
        if not external_path:
            return None
        async with semaphore:
            return await _fetch_one_posting_detail(
                base_url=base_url,
                tenant=tenant,
                site=site,
                external_path=external_path,
            )

    paths = [item.get("externalPath", "") for item in shallow]
    detail_results = await asyncio.gather(
        *(_bounded_detail(p) for p in paths),
        return_exceptions=True,
    )

    jobs: list[StandardJob] = []
    for list_item, detail_result in zip(shallow, detail_results, strict=True):
        if isinstance(detail_result, BaseException):
            logger.warning("workday detail raised: %s", detail_result)
            continue
        if detail_result is None:
            # Detail fetch failed. Drop the posting — see module docstring
            # for the trade-off rationale.
            continue

        external_path = list_item.get("externalPath", "")
        # Prefer the detail endpoint's ``externalUrl`` (the apply page);
        # fall back to constructing it from base + site + path. Note the
        # previous code did ``f"{base_url}/job/{external_path}"`` which
        # produced a broken URL with a doubled ``/job/`` segment whenever
        # ``externalPath`` already started with ``/job/...`` (it always
        # does). The correct construction is base + site + path.
        absolute_url = (
            detail_result.get("externalUrl") or f"{base_url}/{site}{external_path}"
            if external_path
            else ""
        )

        jobs.append(
            StandardJob(
                external_id=external_path or str(list_item.get("bulletFields", [""])[0]),
                title=detail_result.get("title", list_item.get("title", "")),
                location_name=list_item.get("locationsText"),
                content=detail_result.get("jobDescription", ""),
                posted_at=detail_result.get("postedOn", list_item.get("postedOn", "")),
                absolute_url=absolute_url,
            )
        )
    return jobs
