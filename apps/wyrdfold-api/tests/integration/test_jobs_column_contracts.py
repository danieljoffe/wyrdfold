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


@pytest.mark.asyncio
async def test_manual_add_upsert_row_columns(async_service_client) -> None:
    """The manual-add WRITER's actual upsert row lands against real Postgres.

    The R2 column drops broke this path invisibly: ``materialize_and_score_job``
    kept writing the dropped ``jobs.score``/``score_breakdown`` keys, unit
    mocks accepted them, and the failure surfaced only in prod (2026-08-06)
    when the from-url flow's deferred derivation died on PGRST204. Readers got
    contract tests in #556 — this is the writer's. Runs the REAL function
    (``targets=[]`` skips scoring) so any future drop of a column it writes
    fails HERE.
    """
    from app.services.job_ingest import materialize_and_score_job

    url = "https://example.com/e2e-column-contract-manual-add"
    posting_id = await materialize_and_score_job(
        async_service_client,
        final_url=url,
        title="Column Contract Probe",
        company_name="Contract Probe Co",
        location="Remote, US",
        description_html="<p>Writer column-contract probe row.</p>",
        salary_text=None,
        targets=[],
    )
    assert posting_id, "manual-add upsert returned no row — column contract broken"
    await (
        async_service_client.table("jobs").delete().eq("id", posting_id).execute()
    )


def test_jobs_embed_columns_resolve(service_client: Client) -> None:
    """``_JOBS_EMBED`` is a PostgREST embed STRING on the hottest list path.

    It rides every /jobs and dashboard query, so a jobs-column rename (or a
    typo'd addition — ``country`` joined it on 2026-08-08 to fix the inert
    country filter) breaks the whole list, not a best-effort background read.
    Run the real embed against real Postgres so that fails here.
    """
    from app.routers.jobs import _JOBS_EMBED, _SCORE_ROW_COLS

    resp = (
        service_client.table("scores")
        .select(f"{_SCORE_ROW_COLS}{_JOBS_EMBED}")
        .limit(1)
        .execute()
    )
    assert isinstance(resp.data, list)


def test_score_floor_predicate_resolves(service_client: Client) -> None:
    """The floor is an ``or_`` STRING too, and it changed on 2026-08-08 from
    ``scoring_status`` to ``axis_scores`` (they disagreed on 5k+ live rows).
    A column that doesn't exist would make PostgREST 400 the entire list."""
    from app.routers.jobs import _apply_score_floor

    resp = (
        _apply_score_floor(service_client.table("scores").select("job_posting_id"), 70)
        .limit(1)
        .execute()
    )
    assert isinstance(resp.data, list)
