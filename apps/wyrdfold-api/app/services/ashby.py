import logging

from app.http_client import FetchExhaustedError, json_or_none, request_with_retry
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
    try:
        resp = await request_with_retry("GET", url)
    except FetchExhaustedError as exc:
        logger.warning("ashby fetch exhausted retries for %s: %s", slug, exc)
        return []

    if resp.status_code == 404:
        return []
    if resp.status_code >= 400:
        logger.warning("ashby %s returned %d for %s", slug, resp.status_code, url)
        return []

    data = json_or_none(resp, source=f"ashby {slug}")
    if data is None:
        return []
    raw_jobs = data.get("jobs", [])
    if not isinstance(raw_jobs, list):
        return []

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
