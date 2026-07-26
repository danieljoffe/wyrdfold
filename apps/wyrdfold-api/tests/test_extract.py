"""Tests for job metadata extraction service (#500)."""

from app.services.extract import (
    _company_from_domain,
    _extract_from_html_meta,
    _extract_from_jsonld,
    extract_job_from_html,
)

# ---------------------------------------------------------------------------
# Tier 1: JSON-LD extraction
# ---------------------------------------------------------------------------


class TestExtractJsonLD:
    def test_full_job_posting(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        {
            "@type": "JobPosting",
            "title": "Senior Frontend Engineer",
            "description": "<p>Build amazing UIs</p>",
            "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"},
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "addressLocality": "San Francisco",
                    "addressRegion": "CA"
                }
            }
        }
        </script>
        </head></html>
        """
        result = _extract_from_jsonld(html)
        assert result is not None
        assert result.title == "Senior Frontend Engineer"
        assert result.company_name == "Acme Corp"
        assert result.location == "San Francisco, CA"
        assert result.description_html == "<p>Build amazing UIs</p>"
        assert result.tier == "jsonld"

    def test_missing_title_returns_none(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "JobPosting", "description": "Some description"}
        </script>
        </head></html>
        """
        result = _extract_from_jsonld(html)
        assert result is None

    def test_no_jsonld_returns_none(self):
        html = "<html><body>No structured data here</body></html>"
        result = _extract_from_jsonld(html)
        assert result is None

    def test_hiring_organization_missing(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Engineer", "description": "Work here"}
        </script>
        </head></html>
        """
        result = _extract_from_jsonld(html)
        assert result is not None
        assert result.title == "Engineer"
        assert result.company_name is None

    def test_uses_job_title_field(self):
        """Some sites use jobTitle instead of title."""
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "JobPosting", "jobTitle": "Staff Engineer", "description": "x"}
        </script>
        </head></html>
        """
        result = _extract_from_jsonld(html)
        assert result is not None
        assert result.title == "Staff Engineer"


# ---------------------------------------------------------------------------
# Tier 2: HTML meta/OG extraction
# ---------------------------------------------------------------------------


class TestExtractHtmlMeta:
    def test_og_tags(self):
        html = """
        <html><head>
        <meta property="og:title" content="Software Engineer at Stripe" />
        <meta property="og:site_name" content="Stripe Careers" />
        <meta property="og:description" content="Build payment systems" />
        </head><body></body></html>
        """
        result = _extract_from_html_meta(html, "https://stripe.com/jobs/123")
        assert result is not None
        assert result.title == "Software Engineer at Stripe"
        assert result.company_name == "Stripe Careers"
        assert result.description_html == "Build payment systems"
        assert result.tier == "html_meta"

    def test_title_tag_fallback(self):
        html = "<html><head><title>React Developer - Apply Now</title></head><body></body></html>"
        result = _extract_from_html_meta(html, "https://example.com/jobs/1")
        assert result is not None
        assert result.title == "React Developer - Apply Now"

    def test_company_from_domain(self):
        html = "<html><head><title>Some Job</title></head><body></body></html>"
        result = _extract_from_html_meta(html, "https://careers.google.com/jobs/1")
        assert result is not None
        assert result.company_name == "Google"

    def test_no_title_returns_none(self):
        html = "<html><head></head><body>No title at all</body></html>"
        result = _extract_from_html_meta(html, "https://example.com")
        assert result is None

    def test_description_from_content_area(self):
        html = """
        <html><head><title>Engineer</title></head><body>
        <div class="job-description">
            <p>Requirements: 5 years experience</p>
        </div>
        </body></html>
        """
        result = _extract_from_html_meta(html, "https://example.com/jobs/1")
        assert result is not None
        assert "Requirements" in (result.description_html or "")


# ---------------------------------------------------------------------------
# Company from domain
# ---------------------------------------------------------------------------


class TestCompanyFromDomain:
    def test_jobs_subdomain(self):
        assert _company_from_domain("https://jobs.stripe.com/123") == "Stripe"

    def test_careers_subdomain(self):
        assert _company_from_domain("https://careers.google.com/jobs") == "Google"

    def test_www_prefix(self):
        assert _company_from_domain("https://www.example.com/careers") == "Example"

    def test_bare_domain(self):
        assert _company_from_domain("https://netflix.com/jobs") == "Netflix"


# ---------------------------------------------------------------------------
# Full cascade
# ---------------------------------------------------------------------------


class TestExtractCascade:
    def test_jsonld_stops_at_tier1(self):
        html = """
        <html><head>
        <title>Fallback Title</title>
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "JSON-LD Title", "description": "desc"}
        </script>
        </head></html>
        """
        result = extract_job_from_html(html, "https://example.com/jobs/1")
        assert result.tier == "jsonld"
        assert result.title == "JSON-LD Title"

    def test_no_jsonld_falls_to_tier2(self):
        html = """
        <html><head>
        <meta property="og:title" content="OG Title" />
        </head><body></body></html>
        """
        result = extract_job_from_html(html, "https://example.com/jobs/1")
        assert result.tier == "html_meta"
        assert result.title == "OG Title"

    def test_empty_page_returns_none_tier(self):
        result = extract_job_from_html("", "https://example.com")
        assert result.tier == "none"
        assert result.title is None

    def test_no_metadata_returns_none_tier(self):
        html = "<html><head></head><body>Just text</body></html>"
        result = extract_job_from_html(html, "https://example.com")
        assert result.tier == "none"
        assert "extraction_failed" in result.warnings[0]


# ---------------------------------------------------------------------------
# Salary extraction v2 (#503) — regression corpus from REAL prod formats +
# the new structural/single-bound/currency capabilities.
# ---------------------------------------------------------------------------

import pytest

from app.services.extract import extract_salary_from_html, extract_salary_from_text


class TestSalaryProdRegressionCorpus:
    """Every format prod has ACTUALLY matched must keep matching (sampled from
    the live jobs table, 2026-07-26). Grow this list with every real miss."""

    @pytest.mark.parametrize(
        "text",
        [
            "$120,000-$275,000",
            "$125,000.00 - $145,000.00/per year",
            "$77,600.00 to $176,000.00",
            "$200K – $400K",  # en-dash, K-suffix
            "$59.10 to $65.50",
            "$100k to $500k",
            "$17.89 - $26.35 / Hour",
            "$18.47 - $18.97 per hour",
            "$19.00 - 21.00",  # second amount without '$'
            "$34.00 - $40.00/hour",
            "$49 - $57",  # unit-less hourly range (USC) — ranges are not value-gated
        ],
    )
    def test_prod_format_still_matches_exactly(self, text: str):
        assert extract_salary_from_text(f"Compensation: {text} for this role") == text

    def test_trailing_comma_is_trimmed(self):
        # Prod once stored "$148,000 - $185,000," verbatim.
        assert (
            extract_salary_from_text("pays $148,000 - $185,000, plus equity")
            == "$148,000 - $185,000"
        )

    def test_range_keeps_trailing_iso_code(self):
        # The Greenhouse pay-range text (Reddit): the USD suffix now survives.
        assert (
            extract_salary_from_text("is: $190,800 — $267,100 USD")
            == "$190,800 — $267,100 USD"
        )


class TestSalaryNewCapabilities:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Salary up to $180,000 DOE", "up to $180,000"),
            ("From £60,000 per annum", "From £60,000 per annum"),
            ("starting at $25/hour", "starting at $25/hour"),
            ("base from $150k", "from $150k"),
            ("£60,000 - £75,000", "£60,000 - £75,000"),
            ("€70,000 to €90,000", "€70,000 to €90,000"),
            ("CA$120,000 - CA$150,000 CAD", "CA$120,000 - CA$150,000 CAD"),
        ],
    )
    def test_new_forms_match(self, text: str, expected: str):
        assert extract_salary_from_text(f"Pay: {text}.") == expected

    def test_range_wins_over_single_bound_prefix(self):
        # "from $150k to $190k" must yield the whole range, not the bound.
        assert extract_salary_from_text("from $150k to $190k") == "$150k to $190k"


class TestSalaryFalsePositiveGuards:
    @pytest.mark.parametrize(
        "text",
        [
            "we raised $30M in Series B",  # funding, no range/cue
            "401(k) with employer match",  # no currency amount
            "a $5 coffee budget",  # bare small amount, no cue
            "up to $500 wellness stipend",  # cue word but perk-sized, no hourly unit
            "up to 20 days PTO",  # no currency at all
        ],
    )
    def test_non_salaries_do_not_match(self, text: str):
        assert extract_salary_from_text(text) is None


class TestSalaryFromHtmlStructural:
    # The verbatim Greenhouse pay-transparency structure (Reddit, post-#500
    # unescaped form): the REAL comp block must win over earlier prose figures.
    _PAY_DIV = (
        '<div class="content-pay-transparency"><div class="pay-input">'
        '<div class="title">The base salary range for this position is:</div>'
        '<div class="pay-range"><span>$190,800</span>'
        '<span class="divider">—</span>'
        "<span>$267,100 USD</span></div></div></div>"
    )

    def test_pay_range_block_is_parsed_structurally(self):
        html = f"<div><p>About us…</p>{self._PAY_DIV}</div>"
        assert extract_salary_from_html(html) == "$190,800 — $267,100 USD"

    def test_structural_block_wins_over_earlier_prose_figure(self):
        html = (
            "<p>We manage $2,000 - $5,000 ad budgets daily.</p>" + self._PAY_DIV
        )
        # Prose regex alone would grab the ad-budget range first; the
        # structural parse must return the real compensation instead.
        assert extract_salary_from_html(html) == "$190,800 — $267,100 USD"

    def test_stored_escaped_rows_still_extract_via_token_stream(self):
        # Pre-#500 rows: the pay div is HTML-ESCAPED (no literal 'pay-range'
        # class match for bs4) — the strip_html double-pass fallback recovers it.
        escaped = (
            "&lt;div class=&quot;pay-range&quot;&gt;&lt;span&gt;$190,800&lt;/span&gt;"
            "&lt;span class=&quot;divider&quot;&gt;&amp;mdash;&lt;/span&gt;"
            "&lt;span&gt;$267,100 USD&lt;/span&gt;&lt;/div&gt;"
        )
        got = extract_salary_from_html(escaped)
        assert got is not None and "$190,800" in got and "$267,100" in got

    def test_none_and_salary_free_html(self):
        assert extract_salary_from_html(None) is None
        assert extract_salary_from_html("<p>Great benefits and PTO.</p>") is None
