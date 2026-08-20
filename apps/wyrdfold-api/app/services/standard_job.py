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

    # ---- Board-published metadata (#846) ---------------------------------
    # Facts the ATS itself publishes as structured fields. We used to infer
    # all of these with an LLM (the qualification tagger writes
    # jobs.is_remote / employment_type; Phase 2 writes
    # scores.logistics_filters.remote_status / country) — which cost money to
    # guess at the employer's own answer, and produced disagreements between
    # the two inference paths (#795: 229 prod contradictions on remote alone).
    #
    # Populated ONLY where the provider actually publishes the field:
    #   ashby           isRemote / workplaceType, employmentType, department,
    #                   address.postalAddress.addressCountry
    #   lever           workplaceType, country (already ISO-2),
    #                   categories.commitment / .department
    #   smartrecruiters location.remote + location.hybrid, location.country,
    #                   typeOfEmployment, department
    # greenhouse and workday publish none of it in their cheap path — they get
    # the deterministic location parser instead (#846 step 2).
    #
    # None means "this board didn't say", NOT "false". Write sites must leave
    # the column untouched on None so a board silence never blanks a value
    # some other path already established.
    is_remote: bool | None = None
    country: str | None = None  # ISO 3166-1 alpha-2, upper-case
    employment_type: str | None = None  # jobs.employment_type vocabulary
    department: str | None = None
