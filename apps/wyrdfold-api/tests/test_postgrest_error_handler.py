"""PostgREST error-mapping regression tests.

A malformed UUID path id (``GET /jobs/not-a-uuid``) makes PostgREST reject the
cast with ``22P02 invalid input syntax for type uuid``. Uncaught, that
``APIError`` used to surface as a **500** — a *server* error for what is really
client-supplied garbage (500 log noise, and the wrong contract: the resource
simply isn't there). ``_postgrest_error_handler`` maps ``22P02`` to a clean
**404** (identical to a well-formed-but-absent id, and the only status the web
UI renders as a calm "not found" page rather than a red error toast), while
every other PostgREST error still 500s unchanged.

Found in the 2026-07-19 front-end red-team: ``/jobs/<garbage>`` returned 500.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app.config import settings
from app.dependencies import (
    get_async_supabase_for_caller,
    get_current_user_id,
    verify_api_key_or_jwt,
)
from app.main import app


def _api_error(code: str | None, message: str = "boom") -> APIError:
    payload: dict[str, Any] = {"message": message, "details": None, "hint": None}
    if code is not None:
        payload["code"] = code
    return APIError(payload)


# A code-less APIError must resolve to ``code is None`` so it falls through to
# the 500 path — i.e. the handler keys off an explicit "22P02", never swallows
# an arbitrary error as 404.
def test_apierror_without_code_is_none() -> None:
    assert _api_error(None).code is None


# ---------------------------------------------------------------------------
# Handler behaviour through the full app stack (throwaway routes, no DB).
# ---------------------------------------------------------------------------


async def _raise_22p02() -> None:
    raise _api_error("22P02", 'invalid input syntax for type uuid: "not-a-uuid" /internal')


async def _raise_other() -> None:
    # 23505 = unique_violation: a genuine server-side error, must stay a 500.
    raise _api_error("23505", "duplicate key value violates unique constraint")


app.add_api_route("/__test/pg-22p02", _raise_22p02, methods=["GET"])
app.add_api_route("/__test/pg-other", _raise_other, methods=["GET"])


def test_22p02_maps_to_404() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/__test/pg-22p02")
    assert res.status_code == 404
    assert res.json()["detail"] == "Not found"


def test_22p02_404_body_leaks_no_postgrest_detail() -> None:
    """The raw PostgREST message echoes the offending value + the column type —
    it must never reach the client."""
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/__test/pg-22p02")
    assert "invalid input syntax" not in res.text
    assert "uuid" not in res.text.lower()
    assert "/internal" not in res.text


def test_non_22p02_apierror_still_500s() -> None:
    """Every other PostgREST error re-raises to the generic 500 handler (with
    its fail-closed body), unchanged — the mapping is scoped to malformed input,
    not a blanket "PostgREST error -> 404"."""
    assert settings.debug_errors is False  # default fail-closed posture
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/__test/pg-other")
    assert res.status_code == 500
    assert res.json()["detail"] == "Internal server error"
    # No raw PostgREST internals leak on the 500 path either.
    assert "unique constraint" not in res.text
    assert "23505" not in res.text


# ---------------------------------------------------------------------------
# The real route benefits: GET /jobs/{id} whose ownership query raises 22P02
# (exactly as PostgREST does for a malformed uuid) returns 404, not 500.
# ---------------------------------------------------------------------------


def test_get_job_malformed_uuid_returns_404_not_500() -> None:
    supabase = MagicMock()
    # _assert_user_owns_posting does:
    #   table("jobs").select(...).eq("id", posting_id).limit(1).execute()
    supabase.table.return_value.select.return_value.eq.return_value.limit.return_value.execute = (
        AsyncMock(side_effect=_api_error("22P02", 'invalid input syntax for type uuid: "not-a-uuid"'))
    )

    # The jobs router gates every route with ``verify_api_key_or_jwt``; override
    # it (and the per-route auth + client deps) so the request reaches the
    # handler and the ownership query — where the simulated 22P02 fires.
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "jwt"
    app.dependency_overrides[get_current_user_id] = lambda: "user-a"
    app.dependency_overrides[get_async_supabase_for_caller] = lambda: supabase
    try:
        res = TestClient(app, raise_server_exceptions=False).get("/jobs/not-a-uuid")
        assert res.status_code == 404
        assert res.json()["detail"] == "Not found"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 57014 statement timeout -> 503 with Retry-After (#604). The database
# refusing work under load is not a server bug: an unhandled 500 dumped a
# full ExceptionGroup traceback per occurrence and told the retry layers the
# request itself was broken. 503 is the honest, retryable contract.
# ---------------------------------------------------------------------------


async def _raise_57014() -> None:
    raise _api_error("57014", "canceling statement due to statement timeout")


app.add_api_route("/__test/pg-57014", _raise_57014, methods=["GET"])


def test_57014_maps_to_503_with_retry_after() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/__test/pg-57014")
    assert res.status_code == 503
    assert res.headers.get("Retry-After") == "5"
    assert res.json()["detail"] == "Temporarily overloaded — please retry."
    # No PostgREST internals leak.
    assert "canceling statement" not in res.text
    assert "57014" not in res.text
