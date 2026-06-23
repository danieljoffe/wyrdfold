"""Information-disclosure regression tests (audit #29 round 3).

H5 — the unhandled-exception (500) handler must FAIL CLOSED: a generic
     body by default; raw exception text only when DEBUG_ERRORS is
     explicitly opted into.
H8 — the SSRF validator's rejections must not echo the resolved internal
     host/IP back to the client (recon oracle).
M4 — tailored-resume download must not leak raw Storage/pandoc exception
     text to the client.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


# A throwaway route whose handler raises an exception carrying secret-ish
# detail, so we can assert what the 500 handler echoes to the client.
async def _boom() -> None:
    raise RuntimeError("SECRET sql: SELECT * FROM users WHERE token='abc' /srv/app")


app.add_api_route("/__test/boom", _boom, methods=["GET"])


# ---------------------------------------------------------------------------
# H5 — 500 handler is fail-closed
# ---------------------------------------------------------------------------


def test_500_is_generic_by_default() -> None:
    """With DEBUG_ERRORS unset (the default), the 500 body is generic and
    leaks no exception type / SQL / file path."""
    assert settings.debug_errors is False  # default posture
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/__test/boom")
    assert res.status_code == 500
    body = res.json()
    assert body["detail"] == "Internal server error"
    # None of the secret-bearing exception text leaks.
    assert "RuntimeError" not in res.text
    assert "SECRET" not in res.text
    assert "SELECT" not in res.text
    assert "/srv/app" not in res.text
    # The path is still surfaced (non-sensitive, useful for the BFF).
    assert body["path"] == "/__test/boom"


def test_500_is_verbose_only_when_debug_errors_opted_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The explicit DEBUG_ERRORS opt-in restores verbose detail for local
    debugging — proving the gate is a real switch, not a no-op."""
    monkeypatch.setattr(settings, "debug_errors", True)
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/__test/boom")
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert "RuntimeError" in detail
    assert "SECRET" in detail


# ---------------------------------------------------------------------------
# H8 — SSRF rejection messages are generic
# ---------------------------------------------------------------------------


def test_manual_job_ssrf_rejection_is_generic(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A URL whose host resolves to an internal address is refused with a
    generic message — the resolved host/IP is never reflected to the client
    (POST /jobs/manual exercises the assert_safe_host reflection site)."""
    import ipaddress
    from unittest.mock import MagicMock

    import app.services.validate as validate_mod

    # Resolve everything to the cloud-metadata address so assert_safe_host
    # rejects it. (conftest's autouse fixture stubs resolution to public; we
    # re-stub here to force the SSRF path.)
    def _internal(_hostname: str):  # type: ignore[no-untyped-def]
        return [ipaddress.ip_address("169.254.169.254")]

    monkeypatch.setattr(validate_mod, "_resolve_addresses", _internal)

    from app.dependencies import (
        get_current_user_id_optional,
        get_supabase,
        get_supabase_for_caller,
        verify_api_key_or_jwt,
    )

    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_supabase_for_caller] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id_optional] = lambda: "user-a"
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "user-a"
    try:
        client = TestClient(app, raise_server_exceptions=False)
        res = client.post(
            "/jobs/manual",
            json={"url": "http://metadata.internal.example/latest"},
        )
        # Refused (not fetched).
        assert res.status_code == 400
        detail = res.json()["detail"]
        assert detail == "This URL cannot be fetched"
        # The resolved internal IP / host must NOT appear in the response.
        assert "169.254.169.254" not in res.text
        assert "metadata.internal.example" not in res.text
        assert "disallowed" not in res.text.lower()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# M4 — tailored-resume download does not leak raw Storage exception text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_download_storage_error_is_generic() -> None:
    """A Storage download failure must surface a generic message; the raw
    exception (which can carry the internal Storage path) stays server-side
    (audit #29 R3 / M4)."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock, patch

    from fastapi import HTTPException

    from app.models.tailor import TailoredResumeRecord
    from app.routers import tailor as tailor_router

    # Legacy row: storage_path set, no payload_md → the route serves cached
    # bytes via download_docx, which we force to raise.
    record = TailoredResumeRecord(
        id="rec-1",
        user_id="user-a",
        job_posting_id="job-1",
        document_type="resume",
        resume_type="generic",
        jd_snapshot="JD",
        jd_snapshot_hash="h",
        payload={"summary": "s", "contact": {"name": "n", "email": "e@x.com"}},
        payload_md=None,
        docx_payload_md_hash=None,
        storage_path="user-a/secret-internal-path/rec-1.docx",
        warnings=[],
        model="m",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        latency_ms=1,
        created_at=datetime.now(UTC),
        approved_at=None,
    )

    secret = "Storage 403 at user-a/secret-internal-path/rec-1.docx token=XYZ"

    with (
        patch("app.services.tailor.persistence.get", return_value=record),
        patch(
            "app.services.tailor.persistence.download_docx",
            side_effect=RuntimeError(secret),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await tailor_router.download_tailored_resume(
                resume_id="rec-1",
                supabase=MagicMock(),
                user_supabase=MagicMock(),
                user_id="user-a",
            )

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 502
    assert detail == "failed to fetch resume document"
    # The raw exception (internal path, token) must not be in the client body.
    assert "secret-internal-path" not in detail
    assert "token=XYZ" not in detail
    assert "RuntimeError" not in detail
