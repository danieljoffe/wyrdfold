import logging

from app.http_client import (
    BoardFetchError,
    FetchExhaustedError,
    board_list_json,
    request_with_retry,
)
from app.services.board_metadata import (
    normalize_country,
    normalize_employment_type,
    normalize_remote,
)
from app.services.standard_job import StandardJob

logger = logging.getLogger(__name__)

ASHBY_BASE = "https://api.ashbyhq.com/posting-api/job-board"


async def fetch_ashby_jobs(slug: str) -> list[StandardJob]:
    url = f"{ASHBY_BASE}/{slug}"
    source = f"ashby {slug}"
    try:
        resp = await request_with_retry("GET", url)
    except FetchExhaustedError as exc:
        logger.warning("ashby fetch exhausted retries for %s: %s", slug, exc)
        raise BoardFetchError(f"{source} exhausted retries", source=source) from exc

    data = board_list_json(resp, source=source)
    raw_jobs = data.get("jobs", [])
    if not isinstance(raw_jobs, list):
        # A 200 whose ``jobs`` is not a list is a malformed response, not a
        # board with nothing open.
        raise BoardFetchError(
            f"{source} returned 200 with a non-list 'jobs'",
            source=source,
            status=resp.status_code,
        )

    jobs: list[StandardJob] = []
    for item in raw_jobs:
        # Board-published metadata (#846) — Ashby states these outright, so
        # there is nothing for the LLM to infer. ``address`` is a nested
        # schema.org postalAddress.
        postal = (item.get("address") or {}).get("postalAddress") or {}
        jobs.append(
            StandardJob(
                external_id=str(item["id"]),
                title=item.get("title", ""),
                location_name=item.get("location"),
                content=item.get("descriptionHtml", ""),
                posted_at=item.get("publishedAt", ""),
                absolute_url=item.get("jobUrl", ""),
                is_remote=normalize_remote(
                    is_remote=item.get("isRemote"),
                    workplace_type=item.get("workplaceType"),
                ),
                country=normalize_country(postal.get("addressCountry")),
                employment_type=normalize_employment_type(item.get("employmentType")),
                department=item.get("department") or item.get("team"),
            )
        )
    return jobs
