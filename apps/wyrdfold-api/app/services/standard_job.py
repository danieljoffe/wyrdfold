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
    # True when the provider's per-posting DETAIL fetch was deliberately
    # skipped because we already hold this posting unchanged (Workday only —
    # every other provider returns content in its list call). The posting is
    # still RETURNED so the stale-archive pass counts it as seen; the poller
    # excludes it from the upsert, because ``content`` is empty and writing it
    # would blank the stored description.
    detail_skipped: bool = False
