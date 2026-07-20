"""BFF-only shared secret on the public IP-rate-limited endpoints (SEC-5).

The waitlist-join and signup-mode endpoints are rate-limited per client IP, but
``FORWARDED_ALLOW_IPS="*"`` lets a direct hit to the API forge ``X-Forwarded-For``
and rotate past the limit. ``require_bff_secret`` makes them BFF-only: the BFF
injects ``X-Wyrdfold-BFF`` and the API requires it — *when configured*. These
pin the guard (fail-open when unset, constant-time match when set) and its
wiring onto both endpoints.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.dependencies import get_settings, get_supabase, require_bff_secret
from app.main import app
from app.rate_limit import limiter


class _Req:
    """Minimal stand-in exposing ``.headers.get(key, default)``."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def teardown_function() -> None:
    app.dependency_overrides.clear()
    limiter.reset()


# ---- unit: the guard itself -------------------------------------------------


def test_guard_fails_open_when_secret_unset() -> None:
    # No secret configured → no-op (rollout safety), whatever header is sent.
    require_bff_secret(_Req({}), Settings(wyrdfold_bff_secret=""))  # no raise


def test_guard_rejects_missing_header_when_secret_set() -> None:
    with pytest.raises(HTTPException) as exc:
        require_bff_secret(_Req({}), Settings(wyrdfold_bff_secret="s3cret"))
    assert exc.value.status_code == 403


def test_guard_rejects_wrong_header_when_secret_set() -> None:
    with pytest.raises(HTTPException) as exc:
        require_bff_secret(
            _Req({"x-wyrdfold-bff": "nope"}),
            Settings(wyrdfold_bff_secret="s3cret"),
        )
    assert exc.value.status_code == 403


def test_guard_accepts_correct_header() -> None:
    require_bff_secret(
        _Req({"x-wyrdfold-bff": "s3cret"}),
        Settings(wyrdfold_bff_secret="s3cret"),
    )  # no raise


# ---- integration: the guard is wired onto both public endpoints -------------


def _client(secret: str) -> TestClient:
    app.dependency_overrides[get_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_settings] = lambda: Settings(wyrdfold_bff_secret=secret)
    return TestClient(app)


def test_waitlist_rejects_direct_hit_without_secret_when_configured() -> None:
    resp = _client("s3cret").post("/waitlist", json={"email": "jane@example.com"})
    assert resp.status_code == 403


def test_waitlist_accepts_request_carrying_the_secret() -> None:
    resp = _client("s3cret").post(
        "/waitlist",
        json={"email": "jane@example.com"},
        headers={"x-wyrdfold-bff": "s3cret"},
    )
    assert resp.status_code != 403  # guard passed → endpoint ran


def test_waitlist_open_when_secret_unset() -> None:
    # Fail-open: a not-yet-rolled-out deploy must not hard-break public signup.
    resp = _client("").post("/waitlist", json={"email": "jane@example.com"})
    assert resp.status_code != 403


def test_signup_mode_rejects_direct_hit_without_secret_when_configured() -> None:
    resp = _client("s3cret").get("/signup-mode")
    assert resp.status_code == 403


def test_signup_mode_accepts_request_carrying_the_secret() -> None:
    resp = _client("s3cret").get("/signup-mode", headers={"x-wyrdfold-bff": "s3cret"})
    assert resp.status_code != 403
