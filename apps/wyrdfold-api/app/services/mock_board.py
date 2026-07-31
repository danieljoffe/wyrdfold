"""Synthetic job-board fetcher for local load testing (#57).

The real fetchers all talk to external ATS APIs, which makes a poll burst
impossible to reproduce locally: fake board tokens 404, and pointing a load
test at real boards is rude, slow, and non-deterministic. This provider
synthesizes a feed entirely in-process so the #57 before/after load runs
exercise the exact ingestion + scoring write herd on identical inputs.

Deterministic by design: the same ``board_token`` always yields the same
jobs (ids, titles, locations, descriptions), so two runs of the rig upsert
identical rows and the flag-off/flag-on runs can be diffed for correctness,
not just latency. Titles cycle through a fixed vocabulary — seed the load
test's target with keywords from ``_TITLE_STEMS`` so the rows pass the
title prematch and actually reach Stage 1/2 scoring.

Token shape: ``anything`` or ``anything:N`` where N is the job count
(default ``_DEFAULT_JOB_COUNT``, capped to ``_MAX_JOB_COUNT``).

Hard-gated behind ``MOCK_FETCHER_ENABLED`` (default off): a real source row
with a mistyped provider must fail its poll — never quietly fabricate jobs
into a live catalog.
"""

from __future__ import annotations

from app.config import settings
from app.services.standard_job import StandardJob

_DEFAULT_JOB_COUNT = 150
_MAX_JOB_COUNT = 2000

# Role stems the synthetic titles cycle through. The load-test target's
# scoring keywords should overlap these (e.g. "customer experience",
# "support operations") so the feed survives the free ingestion gates.
_TITLE_STEMS: tuple[str, ...] = (
    "Customer Experience Manager",
    "Director of Customer Experience",
    "Support Operations Manager",
    "Customer Experience Operations Lead",
    "Head of Customer Support",
    "CX Program Manager",
)

_LOCATIONS: tuple[str, ...] = (
    "Remote, USA",
    "New York, NY",
    "Austin, TX",
    "San Francisco, CA",
    "Chicago, IL",
)

# A few KB of plausible JD text so Stage-2 parsing/scoring does real work
# instead of racing through empty strings.
_DESCRIPTION_TEMPLATE = """
<h2>About the role</h2>
<p>We are hiring a {title} to own the end-to-end customer experience
program. You will partner with support operations, product, and data teams
to improve CSAT, reduce time-to-resolution, and scale our voice-of-customer
loops. This is job {index} on the {token} demo board.</p>
<h2>What you'll do</h2>
<ul>
<li>Lead customer experience strategy across onboarding and support.</li>
<li>Build support operations dashboards and staffing models.</li>
<li>Run quarterly voice-of-customer reviews with senior leadership.</li>
<li>Coach a team of CX specialists and program managers.</li>
</ul>
<h2>What we're looking for</h2>
<ul>
<li>7+ years in customer experience, support operations, or CX program
management roles with increasing scope.</li>
<li>Experience with Zendesk, Salesforce Service Cloud, or similar.</li>
<li>Comfort with SQL and building executive-ready reporting.</li>
</ul>
"""


def _job_count(board_token: str) -> tuple[str, int]:
    """Split ``token[:N]`` into (stable token, bounded job count)."""
    token, _, raw = board_token.partition(":")
    try:
        n = int(raw) if raw else _DEFAULT_JOB_COUNT
    except ValueError:
        n = _DEFAULT_JOB_COUNT
    return token or "mock", max(1, min(n, _MAX_JOB_COUNT))


async def fetch_mock_jobs(board_token: str) -> list[StandardJob]:
    """Return the deterministic synthetic feed for ``board_token``.

    Raises when the flag is off so a stray ``provider='mock'`` row fails
    its poll loudly (the failure-backoff path) instead of inventing data.
    """
    if not settings.mock_fetcher_enabled:
        raise RuntimeError(
            "mock board provider disabled (set MOCK_FETCHER_ENABLED=true "
            "for local load testing only)"
        )
    token, count = _job_count(board_token)
    jobs: list[StandardJob] = []
    for i in range(count):
        title = f"{_TITLE_STEMS[i % len(_TITLE_STEMS)]} {i // len(_TITLE_STEMS) + 1}"
        jobs.append(
            StandardJob(
                external_id=f"{token}-{i}",
                title=title,
                location_name=_LOCATIONS[i % len(_LOCATIONS)],
                content=_DESCRIPTION_TEMPLATE.format(title=title, index=i, token=token),
                posted_at="2026-07-01T00:00:00Z",
                absolute_url=f"https://example.com/{token}/jobs/{i}",
            )
        )
    return jobs
