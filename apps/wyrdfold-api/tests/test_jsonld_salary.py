"""JSON-LD baseSalary fallback (#503 item 4): pure-parse helper + the
poller's flag-gated, bounded fill."""

from unittest.mock import AsyncMock

import pytest

from app.services.jsonld import salary_from_jsonld_html

_PAGE_WITH_RANGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Engineer",
 "baseSalary":{"@type":"MonetaryAmount","currency":"USD",
   "value":{"@type":"QuantitativeValue","minValue":150000,"maxValue":210000,"unitText":"YEAR"}}}
</script></head><body></body></html>
"""

_PAGE_SINGLE_VALUE = """
<script type="application/ld+json">
{"@type":"JobPosting","title":"Engineer",
 "baseSalary":{"currency":"USD","value":{"value":95000,"unitText":"YEAR"}}}
</script>
"""

_PAGE_NO_SALARY = """
<script type="application/ld+json">
{"@type":"JobPosting","title":"Engineer","description":"no pay info"}
</script>
"""

_PAGE_BROKEN_JSON = """
<script type="application/ld+json">{"@type":"JobPosting","title":</script>
"""


def test_range_formats() -> None:
    assert salary_from_jsonld_html(_PAGE_WITH_RANGE) == "$150,000 – $210,000/yr"


def test_single_value_formats() -> None:
    assert salary_from_jsonld_html(_PAGE_SINGLE_VALUE) == "$95,000/yr"


def test_no_salary_and_broken_json_yield_none() -> None:
    assert salary_from_jsonld_html(_PAGE_NO_SALARY) is None
    assert salary_from_jsonld_html(_PAGE_BROKEN_JSON) is None
    assert salary_from_jsonld_html("<html>no jsonld</html>") is None


@pytest.mark.asyncio
async def test_fill_is_inert_when_flag_off(monkeypatch) -> None:
    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "jsonld_salary_enabled", False)
    fetch = AsyncMock()
    monkeypatch.setattr(poller_mod, "fetch_salary_from_posting_page", fetch)
    rows = [{"external_id": "n1", "salary_text": None, "absolute_url": "https://x/1"}]
    await poller_mod._fill_jsonld_salaries(rows, set())
    fetch.assert_not_awaited()
    assert rows[0]["salary_text"] is None


@pytest.mark.asyncio
async def test_fill_targets_new_salaryless_rows_and_respects_cap(monkeypatch) -> None:
    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "jsonld_salary_enabled", True)
    monkeypatch.setattr(live_settings, "jsonld_salary_max_fetches", 2)
    fetch = AsyncMock(return_value="$150,000 – $210,000/yr")
    monkeypatch.setattr(poller_mod, "fetch_salary_from_posting_page", fetch)

    rows = [
        # Known row — skipped (its salary re-derives from content, #514).
        {"external_id": "known-1", "salary_text": None, "absolute_url": "https://x/k"},
        # Already has a salary — skipped.
        {"external_id": "n0", "salary_text": "$1", "absolute_url": "https://x/0"},
        # No URL — skipped.
        {"external_id": "n1", "salary_text": None, "absolute_url": None},
        # Eligible; only the first TWO fetch (cap).
        {"external_id": "n2", "salary_text": None, "absolute_url": "https://x/2"},
        {"external_id": "n3", "salary_text": None, "absolute_url": "https://x/3"},
        {"external_id": "n4", "salary_text": None, "absolute_url": "https://x/4"},
    ]
    await poller_mod._fill_jsonld_salaries(rows, {"known-1"})

    assert fetch.await_count == 2
    fetched_urls = sorted(c.args[0] for c in fetch.await_args_list)
    assert fetched_urls == ["https://x/2", "https://x/3"]
    assert rows[3]["salary_text"] == "$150,000 – $210,000/yr"
    assert rows[4]["salary_text"] == "$150,000 – $210,000/yr"
    assert rows[5]["salary_text"] is None
    assert rows[0]["salary_text"] is None


@pytest.mark.asyncio
async def test_fill_swallows_fetch_failures(monkeypatch) -> None:
    from app.config import settings as live_settings
    from app.services import poller as poller_mod

    monkeypatch.setattr(live_settings, "jsonld_salary_enabled", True)
    fetch = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(poller_mod, "fetch_salary_from_posting_page", fetch)
    rows = [{"external_id": "n1", "salary_text": None, "absolute_url": "https://x/1"}]
    await poller_mod._fill_jsonld_salaries(rows, set())
    assert rows[0]["salary_text"] is None
