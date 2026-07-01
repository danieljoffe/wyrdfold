"""Integration: /jobs logistics filters against the live stack (#86).

Unit tests cover ``_apply_logistics_filter``'s logic on dicts; this proves the
full two-query path end-to-end — the SELECT returns the ``scores.logistics_filters``
jsonb, ``_assemble_jobs_page`` overlays it onto each posting, and the
remote_only / min_salary / country filters drop the right rows — against real
PostgREST + a real jsonb column. Self-skips when the stack is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from supabase import Client

from app.routers.jobs import _list_jobs_for_target_two_query, _LogisticsFilter

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_logistics(service_client: Client) -> Iterator[tuple[str, dict[str, str]]]:
    """One target, four graded jobs with distinct logistics:
    - ``remote_hi``: remote, $180k, US
    - ``onsite_hi``: onsite, $200k, US
    - ``remote_lo``: remote, $90k,  US
    - ``remote_ca``: remote, $180k, CA
    Yields ``(target_id, {label: job_id})``.
    """
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    ids = {k: str(uuid.uuid4()) for k in ("remote_hi", "onsite_hi", "remote_lo", "remote_ca")}
    board_token = f"test-{uuid.uuid4().hex[:12]}"
    logistics = {
        "remote_hi": {"remote_status": "remote", "salary_max": 180000, "location_country": "US"},
        "onsite_hi": {"remote_status": "onsite", "salary_max": 200000, "location_country": "US"},
        "remote_lo": {"remote_status": "remote", "salary_max": 90000, "location_country": "US"},
        "remote_ca": {"remote_status": "remote", "salary_max": 180000, "location_country": "CA"},
    }
    try:
        service_client.table("sources").insert(
            {"id": source_id, "board_token": board_token, "company_name": "Acme", "provider": "greenhouse"}
        ).execute()
        service_client.table("jobs").insert(
            [
                {"id": jid, "external_id": f"ext-{label}", "source_id": source_id,
                 "title": label, "company_name": "Acme"}
                for label, jid in ids.items()
            ]
        ).execute()
        service_client.table("targets").insert({"id": target_id, "label": "Logistics Target"}).execute()
        service_client.table("scores").insert(
            [
                {"job_posting_id": jid, "target_id": target_id, "score": 80,
                 "excluded": False, "scoring_status": "complete",
                 "logistics_filters": logistics[label]}
                for label, jid in ids.items()
            ]
        ).execute()
        yield target_id, ids
    finally:
        service_client.table("sources").delete().eq("id", source_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def _ids(service_client: Client, target_id: str, f: _LogisticsFilter) -> set[str]:
    result = _list_jobs_for_target_two_query(
        service_client, target_id=target_id, page_size=50, sort="score", ascending=False,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={}, logistics=f,
    )
    return {p["title"] for p in result["postings"]}


def test_remote_only_drops_onsite(
    service_client: Client, seeded_logistics: tuple[str, dict[str, str]]
) -> None:
    target_id, _ = seeded_logistics
    got = _ids(service_client, target_id, _LogisticsFilter(remote_only=True))
    assert got == {"remote_hi", "remote_lo", "remote_ca"}  # onsite dropped


def test_min_salary_drops_below(
    service_client: Client, seeded_logistics: tuple[str, dict[str, str]]
) -> None:
    target_id, _ = seeded_logistics
    got = _ids(service_client, target_id, _LogisticsFilter(min_salary=150000))
    assert got == {"remote_hi", "onsite_hi", "remote_ca"}  # $90k dropped


def test_country_drops_mismatch_lenient_on_null(
    service_client: Client, seeded_logistics: tuple[str, dict[str, str]]
) -> None:
    target_id, _ = seeded_logistics
    got = _ids(service_client, target_id, _LogisticsFilter(country="us"))  # case-insensitive
    assert got == {"remote_hi", "onsite_hi", "remote_lo"}  # CA dropped


def test_filters_compose(
    service_client: Client, seeded_logistics: tuple[str, dict[str, str]]
) -> None:
    target_id, _ = seeded_logistics
    got = _ids(
        service_client, target_id,
        _LogisticsFilter(remote_only=True, min_salary=150000, country="US"),
    )
    assert got == {"remote_hi"}  # only the remote, US, >=150k job survives all three


def test_no_filter_returns_all_with_logistics_overlaid(
    service_client: Client, seeded_logistics: tuple[str, dict[str, str]]
) -> None:
    """Sanity: with no filter every row returns AND carries its logistics_filters
    (proving the SELECT + overlay wiring, not just the drop logic)."""
    target_id, _ = seeded_logistics
    result = _list_jobs_for_target_two_query(
        service_client, target_id=target_id, page_size=50, sort="score", ascending=False,
        min_score=None, status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={}, logistics=_LogisticsFilter(),
    )
    by_title = {p["title"]: p for p in result["postings"]}
    assert set(by_title) == {"remote_hi", "onsite_hi", "remote_lo", "remote_ca"}
    assert by_title["remote_hi"]["logistics_filters"]["remote_status"] == "remote"
    assert by_title["onsite_hi"]["logistics_filters"]["salary_max"] == 200000


def test_rpc_fast_path_also_returns_logistics(
    service_client: Client, seeded_logistics: tuple[str, dict[str, str]]
) -> None:
    """The keyset RPC fast path (non-score sort, no floor) now carries
    logistics_filters too (#86), so chips render on created_at/title/company
    sorts, not just the score-sorted two-query view."""
    from app.routers.jobs import _list_jobs_for_target_rpc

    target_id, _ = seeded_logistics
    result = _list_jobs_for_target_rpc(
        service_client, target_id=target_id, page_size=50,
        sort="created_at", ascending=False, min_score=None,
        status=None, company=None, search=None,
        exclude_terms=[], only_terms=[], cursor={},
    )
    by_title = {p["title"]: p for p in result["postings"]}
    assert set(by_title) == {"remote_hi", "onsite_hi", "remote_lo", "remote_ca"}
    assert by_title["remote_hi"]["logistics_filters"]["remote_status"] == "remote"
    assert by_title["onsite_hi"]["logistics_filters"]["salary_max"] == 200000
