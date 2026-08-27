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

LEVER_BASE = "https://api.lever.co/v0/postings"


async def fetch_lever_jobs(company: str) -> list[StandardJob]:
    url = f"{LEVER_BASE}/{company}?mode=json"
    source = f"lever {company}"
    try:
        resp = await request_with_retry("GET", url)
    except FetchExhaustedError as exc:
        logger.warning("lever fetch exhausted retries for %s: %s", company, exc)
        raise BoardFetchError(f"{source} exhausted retries", source=source) from exc

    data = board_list_json(resp, source=source)
    if not isinstance(data, list):
        # Lever's list endpoint returns a bare JSON array. Anything else on a
        # 200 is a malformed response, not an empty board.
        raise BoardFetchError(
            f"{source} returned 200 with a non-list body",
            source=source,
            status=resp.status_code,
        )

    jobs: list[StandardJob] = []
    for item in data:
        categories = item.get("categories", {})
        # Board-published metadata (#846). Lever's top-level ``country`` is
        # already an ISO-2 code, and ``categories`` carries commitment and
        # department beside the location we were already reading.
        jobs.append(
            StandardJob(
                external_id=str(item["id"]),
                title=item.get("text", ""),
                location_name=categories.get("location"),
                content=item.get("description", ""),
                posted_at=str(item.get("createdAt", "")),
                absolute_url=item.get("hostedUrl", ""),
                is_remote=normalize_remote(workplace_type=item.get("workplaceType")),
                country=normalize_country(item.get("country")),
                employment_type=normalize_employment_type(categories.get("commitment")),
                department=categories.get("department") or categories.get("team"),
            )
        )
    return jobs
