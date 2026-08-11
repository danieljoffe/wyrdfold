import html
import logging

from app.http_client import FetchExhaustedError, request_with_retry
from app.services.standard_job import StandardJob

logger = logging.getLogger(__name__)

GREENHOUSE_BASE = "https://boards-api.greenhouse.io/v1/boards"


async def fetch_board_jobs(board_token: str) -> list[StandardJob]:
    url = f"{GREENHOUSE_BASE}/{board_token}/jobs?content=true"
    try:
        resp = await request_with_retry("GET", url)
    except FetchExhaustedError as exc:
        logger.warning("greenhouse fetch exhausted retries for %s: %s", board_token, exc)
        return []

    if resp.status_code == 404:
        return []
    if resp.status_code >= 400:
        logger.warning("greenhouse %s returned %d for %s", board_token, resp.status_code, url)
        return []

    data = resp.json()
    jobs: list[StandardJob] = []
    for item in data.get("jobs", []):
        location = item.get("location", {})
        jobs.append(
            StandardJob(
                external_id=str(item["id"]),
                title=item.get("title", ""),
                location_name=location.get("name") if location else None,
                # The Job Board API delivers `content` HTML-ESCAPED
                # (&lt;div&gt;…), unlike every other board source we ingest.
                # Unescape here so `description_html` stores REAL markup —
                # otherwise the snippet builder's tag-strip faithfully
                # unescapes the entities into literal tag soup on the search
                # cards (prod bug, 2026-07-26).
                content=html.unescape(item.get("content", "")),
                posted_at=item.get("updated_at", ""),
                absolute_url=item.get("absolute_url", ""),
            )
        )
    return jobs
