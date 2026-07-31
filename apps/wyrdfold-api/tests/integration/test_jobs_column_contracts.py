"""PostgREST column-name contracts for background jobs-table readers.

Why this exists (2026-07-31): the R2 rename (created_at → cataloged_at)
silently broke three background paths — the archival due-select, the
ingestion-health newest-job read, and the insights score-trend base — because
PostgREST selects are STRINGS: mypy can't see them, unit mocks accept any
column, and the errors surfaced only as swallowed best-effort 400s in prod.
These run each reader's actual query shape against real Postgres, so a future
jobs-column rename fails HERE instead of silently in prod.
"""

from __future__ import annotations

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def test_archival_due_select_columns(service_client: Client) -> None:
    resp = (
        service_client.table("jobs")
        .select("id")
        .is_("archived_at", "null")
        .lt("cataloged_at", "2100-01-01T00:00:00+00:00")
        .order("cataloged_at", desc=False)
        .limit(1)
        .execute()
    )
    assert isinstance(resp.data, list)


def test_ingestion_health_newest_read_columns(service_client: Client) -> None:
    resp = (
        service_client.table("jobs")
        .select("cataloged_at")
        .order("cataloged_at", desc=True)
        .limit(1)
        .execute()
    )
    assert isinstance(resp.data, list)


def test_insights_trend_base_columns(service_client: Client) -> None:
    resp = (
        service_client.table("jobs")
        .select("id, cataloged_at")
        .gte("cataloged_at", "2000-01-01T00:00:00+00:00")
        .limit(1)
        .execute()
    )
    assert isinstance(resp.data, list)


def test_url_health_due_rpc_shape(service_client: Client) -> None:
    """The failure-first due RPC returns its declared columns."""
    resp = service_client.rpc(
        "due_url_health_jobs",
        {"p_cutoff": "2100-01-01T00:00:00+00:00", "p_batch_size": 1},
    ).execute()
    assert isinstance(resp.data, list)
