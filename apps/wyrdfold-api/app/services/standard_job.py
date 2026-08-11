from dataclasses import dataclass


@dataclass
class StandardJob:
    """Normalized job shape shared across all ATS providers."""

    external_id: str
    title: str
    location_name: str | None
    content: str
    # The provider's best posted/created/updated date (raw string; the
    # poller normalizes via normalize_posted_at into jobs.source_posted_at).
    posted_at: str
    absolute_url: str
    salary_text: str | None = None
