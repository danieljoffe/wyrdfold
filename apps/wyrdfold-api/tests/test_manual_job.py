"""Tests for POST /jobs/manual endpoint (#500)."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.extract import MANUAL_SOURCE_ID


def _mock_response(
    status_code: int = 200,
    text: str = "",
    url: str = "https://example.com/jobs/123",
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.url = httpx.URL(url)
    return resp


def _patch_size_cap_fetch(
    monkeypatch,
    *,
    text: str = "",
    status_code: int = 200,
    url: str = "https://example.com/jobs/123",
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Stub ``get_with_size_cap`` for ``add_manual_job`` tests.

    The endpoint used to call ``client.get`` directly; the fetch was
    moved into ``get_with_size_cap`` to stream and enforce a body-size
    cap (prevents OOM via user-pasted URLs to huge payloads). The
    helper is the right seam to mock from — it returns the
    ``(response, body_bytes)`` tuple the caller decodes.
    """
    from app.routers import jobs as jobs_router

    if side_effect is not None:
        mock = AsyncMock(side_effect=side_effect)
    else:
        body = text.encode("utf-8")
        resp = _mock_response(status_code=status_code, text=text, url=url)
        mock = AsyncMock(return_value=(resp, body))
    monkeypatch.setattr(jobs_router, "get_with_size_cap", mock)
    return mock


JSONLD_HTML = """
<html><head>
<script type="application/ld+json">
{
    "@type": "JobPosting",
    "title": "Senior Engineer",
    "description": "<p>Build things</p>",
    "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"},
    "jobLocation": {
        "@type": "Place",
        "address": {"addressLocality": "New York", "addressRegion": "NY"}
    }
}
</script>
</head></html>
"""

OG_HTML = """
<html><head>
<meta property="og:title" content="Product Designer" />
<meta property="og:site_name" content="Figma" />
<meta property="og:description" content="Design things" />
</head><body></body></html>
"""


class TestManualJobEndpoint:
    @pytest.fixture(autouse=True)
    def _no_active_target(self, monkeypatch):
        """Prevent target scoring from firing in manual-job tests."""
        from app.routers import jobs as jobs_router

        monkeypatch.setattr(jobs_router, "get_active_target", lambda *_a, **_kw: [])

    @pytest.mark.asyncio
    async def test_happy_path_jsonld(self, monkeypatch):
        _patch_size_cap_fetch(monkeypatch, text=JSONLD_HTML)

        mock_supabase = MagicMock()
        mock_upsert = MagicMock()
        mock_upsert.execute = MagicMock(
            return_value=MagicMock(data=[{"id": "posting-uuid-1"}])
        )
        mock_supabase.table.return_value.upsert.return_value = mock_upsert

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="https://example.com/jobs/123")
        result = await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=mock_supabase)

        assert result.success is True
        assert result.posting_id == "posting-uuid-1"
        assert result.extraction_tier == "jsonld"
        assert result.extracted["title"] == "Senior Engineer"
        assert result.needs_manual_fields is False

        # Verify upsert was called with correct source_id
        upsert_call = mock_supabase.table.return_value.upsert.call_args
        row = upsert_call[0][0]
        assert row["source_id"] == MANUAL_SOURCE_ID
        assert row["title"] == "Senior Engineer"
        assert row["company_name"] == "Acme Corp"
        assert row["score"] >= 0

    @pytest.mark.asyncio
    async def test_manual_add_scores_through_gated_caller_client(self, monkeypatch):
        """#6 R2 step 2: a JWT user's manual-add scores their target via the
        gated RPCs on the caller's client, not a direct service-role write."""
        _patch_size_cap_fetch(monkeypatch, text=JSONLD_HTML)

        from datetime import UTC, datetime

        from app.models.targets import JobTarget, ScoringProfile
        from app.routers import jobs as jobs_router

        target = JobTarget(
            id="tgt-1",
            label="X",
            scoring_profile=ScoringProfile(),
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        monkeypatch.setattr(jobs_router, "get_active_for_user", lambda *_a, **_kw: [target])
        monkeypatch.setattr(jobs_router, "update_global_score", lambda *_a, **_kw: None)

        captured: dict[str, object] = {}

        def fake_score(client, **kwargs):
            captured["score_client"] = client
            captured["gated"] = kwargs.get("gated")
            return None

        monkeypatch.setattr(jobs_router, "target_score_and_upsert", fake_score)

        mock_service = MagicMock()
        mock_service.table.return_value.upsert.return_value.execute = MagicMock(
            return_value=MagicMock(data=[{"id": "posting-uuid-r2"}])
        )
        mock_caller = MagicMock()

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="https://example.com/jobs/r2")
        result = await add_manual_job(
            request=MagicMock(),
            body=body,
            user_id="user-a",
            supabase=mock_service,
            caller_supabase=mock_caller,
        )

        assert result.posting_id == "posting-uuid-r2"
        # Per-target scoring ran on the caller's client, gated.
        assert captured["score_client"] is mock_caller
        assert captured["gated"] is True
        # Force-include went through the gated RPC on the caller's client.
        rpc_names = [c.args[0] for c in mock_caller.rpc.call_args_list]
        assert "user_set_scores_included" in rpc_names

    @pytest.mark.asyncio
    async def test_user_overrides(self, monkeypatch):
        # Page with OG tags, but user provides their own title
        _patch_size_cap_fetch(monkeypatch, text=OG_HTML)

        mock_supabase = MagicMock()
        mock_supabase.table.return_value.upsert.return_value.execute.return_value = (
            MagicMock(data=[{"id": "posting-uuid-2"}])
        )

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(
            url="https://example.com/jobs/456",
            title="My Custom Title",
            company_name="Override Corp",
        )
        result = await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=mock_supabase)

        assert result.success is True
        # User overrides should win
        upsert_call = mock_supabase.table.return_value.upsert.call_args
        row = upsert_call[0][0]
        assert row["title"] == "My Custom Title"
        assert row["company_name"] == "Override Corp"

    @pytest.mark.asyncio
    async def test_malformed_url(self, mock_http_client):
        from fastapi import HTTPException

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="not-a-url")
        with pytest.raises(HTTPException) as exc_info:
            await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=MagicMock())
        assert exc_info.value.status_code == 400
        assert "Malformed" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_banned_domain(self, mock_http_client):
        from fastapi import HTTPException

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="https://www.ziprecruiter.com/jobs/123")
        with pytest.raises(HTTPException) as exc_info:
            await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=MagicMock())
        assert exc_info.value.status_code == 400
        assert "Banned" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_extraction_fails_needs_manual_fields(self, monkeypatch):
        # Empty page with no extractable metadata
        _patch_size_cap_fetch(monkeypatch, text="<html><body>Nothing</body></html>")

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="https://example.com/opaque-page")
        result = await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=MagicMock())

        assert result.success is False
        assert result.needs_manual_fields is True
        assert result.posting_id is None

    @pytest.mark.asyncio
    async def test_extraction_fails_with_user_title_succeeds(self, monkeypatch):
        # Empty page but user provides a title
        _patch_size_cap_fetch(monkeypatch, text="<html><body>Nothing</body></html>")

        mock_supabase = MagicMock()
        mock_supabase.table.return_value.upsert.return_value.execute.return_value = (
            MagicMock(data=[{"id": "posting-uuid-3"}])
        )

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(
            url="https://example.com/opaque-page",
            title="Manually Entered Job",
            company_name="Some Company",
        )
        result = await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=mock_supabase)

        assert result.success is True
        assert result.posting_id == "posting-uuid-3"

    @pytest.mark.asyncio
    async def test_dedup_same_url(self, monkeypatch):
        """Same URL should generate same external_id."""
        import hashlib

        url = "https://example.com/jobs/same"
        expected_id = str(int(hashlib.sha256(url.encode()).hexdigest()[:15], 16))

        _patch_size_cap_fetch(monkeypatch, text=JSONLD_HTML, url=url)

        mock_supabase = MagicMock()
        mock_supabase.table.return_value.upsert.return_value.execute.return_value = (
            MagicMock(data=[{"id": "uuid"}])
        )

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url=url)
        await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=mock_supabase)

        upsert_call = mock_supabase.table.return_value.upsert.call_args
        row = upsert_call[0][0]
        assert row["external_id"] == expected_id

    @pytest.mark.asyncio
    async def test_fetch_error(self, monkeypatch):
        _patch_size_cap_fetch(
            monkeypatch, side_effect=httpx.ConnectError("Connection refused")
        )

        from fastapi import HTTPException

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="https://example.com/jobs/123")
        with pytest.raises(HTTPException) as exc_info:
            await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=MagicMock())
        assert exc_info.value.status_code == 400
        assert "fetch" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_redirect_to_banned(self, monkeypatch):
        _patch_size_cap_fetch(
            monkeypatch, text="", url="https://www.ziprecruiter.com/redirect"
        )

        from fastapi import HTTPException

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="https://legit.com/jobs/123")
        with pytest.raises(HTTPException) as exc_info:
            await add_manual_job(request=MagicMock(), body=body, user_id=None, supabase=MagicMock())
        assert exc_info.value.status_code == 400
        assert "banned" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_ensures_manual_source_before_job_upsert(self, monkeypatch):
        """The manual pseudo-source is upserted before the job, so a missing
        ``sources`` row no longer FK-500s the job insert (Finding 1)."""
        _patch_size_cap_fetch(monkeypatch, text=JSONLD_HTML)

        mock_supabase = MagicMock()
        mock_supabase.table.return_value.upsert.return_value.execute.return_value = (
            MagicMock(data=[{"id": "posting-uuid-1"}])
        )

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="https://example.com/jobs/123")
        result = await add_manual_job(
            request=MagicMock(), body=body, user_id=None, supabase=mock_supabase
        )

        assert result.success is True

        # The "sources" table was upserted (self-healing) before "jobs".
        table_names = [c.args[0] for c in mock_supabase.table.call_args_list]
        assert "sources" in table_names
        assert "jobs" in table_names
        assert table_names.index("sources") < table_names.index("jobs")

        # The seeded source row carries the manual id and is poller-safe.
        from app.services.extract import MANUAL_SOURCE_ID

        source_upsert = mock_supabase.table.return_value.upsert.call_args_list[0]
        source_row = source_upsert.args[0]
        assert source_row["id"] == MANUAL_SOURCE_ID
        assert source_row["enabled"] is False
        assert 5 <= source_row["poll_interval_minutes"] <= 10080

    @pytest.mark.asyncio
    async def test_db_error_returns_clean_502_not_raw_postgres(self, monkeypatch):
        """A DB/PostgREST failure on persist surfaces a clean 502 rather than
        leaking the raw Postgres FK-violation string to the client (Finding 1)."""
        from postgrest.exceptions import APIError

        _patch_size_cap_fetch(monkeypatch, text=JSONLD_HTML)

        raw_pg_message = (
            'insert or update on table "jobs" violates foreign key constraint '
            '"job_postings_source_id_fkey" ... Key (source_id)='
            "(00000000-0000-4000-a000-000000000001) is not present in table "
            '"sources".'
        )
        mock_supabase = MagicMock()
        mock_supabase.table.return_value.upsert.return_value.execute.side_effect = (
            APIError({"message": raw_pg_message})
        )

        from fastapi import HTTPException

        from app.models.schemas import ManualJobRequest
        from app.routers.jobs import add_manual_job

        body = ManualJobRequest(url="https://example.com/jobs/123")
        with pytest.raises(HTTPException) as exc_info:
            await add_manual_job(
                request=MagicMock(), body=body, user_id=None, supabase=mock_supabase
            )

        assert exc_info.value.status_code == 502
        # No raw Postgres internals leak to the client.
        assert "foreign key" not in exc_info.value.detail.lower()
        assert "source_id" not in exc_info.value.detail
        assert "sources" not in exc_info.value.detail.lower()
