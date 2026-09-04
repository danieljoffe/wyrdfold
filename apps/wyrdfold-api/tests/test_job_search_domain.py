"""#470: the search read path carries the source row's verified domain."""

from __future__ import annotations

from app.models.job_search import JobSearchResult
from app.services.job_search import _SEARCH_COLS, _flatten_source_domain


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "j1",
        "title": "Engineer",
        "company_name": "Datadog",
        "location": None,
        "city": None,
        "state": None,
        "country": None,
        "location_remote": None,
        "salary_text": None,
    }
    row.update(overrides)
    return row


def test_embed_flattens_to_company_domain() -> None:
    row = _flatten_source_domain(_row(sources={"domain": "datadoghq.com"}))
    assert row["company_domain"] == "datadoghq.com"
    assert "sources" not in row
    assert JobSearchResult.model_validate(row).company_domain == "datadoghq.com"


def test_missing_embed_and_null_domain_both_read_none() -> None:
    # LEFT join: a job may carry no source row (embed None) or a source
    # with no enriched domain — both must land as None, never a KeyError.
    assert _flatten_source_domain(_row(sources=None))["company_domain"] is None
    assert _flatten_source_domain(_row())["company_domain"] is None
    assert _flatten_source_domain(_row(sources={"domain": None}))["company_domain"] is None


def test_select_embeds_sources_without_inner() -> None:
    """The embed must stay a LEFT join — ``sources!inner`` would silently
    DROP every job whose source lacks a domain row from public search."""
    assert "sources(domain)" in _SEARCH_COLS
    assert "sources!inner" not in _SEARCH_COLS
