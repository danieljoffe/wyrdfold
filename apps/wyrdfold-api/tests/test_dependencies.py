from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException, Request
from jwt import PyJWKClientError

from app.config import Settings
from app.dependencies import (
    _api_key_matches,
    enforce_llm_budget,
    get_async_supabase_for_caller,
    get_current_user_id,
    get_current_user_id_optional,
    refresh_jwks_cache,
    try_decode_jwt_sub_cache_only,
    verify_api_key,
    verify_api_key_or_jwt,
    verify_supabase_jwt,
)

USER_SUB = "11111111-1111-1111-1111-111111111111"
TEST_SUPABASE_URL = "https://test-project.supabase.co"
TEST_ISSUER = f"{TEST_SUPABASE_URL}/auth/v1"
TEST_KID = "test-kid-1"

# Ephemeral EC P-256 keypair used by the whole module. Mirrors the asymmetric
# (ES256) signing model Supabase uses for access tokens. A second keypair is
# generated for "wrong signature" coverage.
_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()

_OTHER_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER_PRIVATE_PEM = _OTHER_PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)


class _FakeSigningKey:
    def __init__(self, key: Any) -> None:
        self.key = key


class _FakeJWKSClient:
    """Stand-in for PyJWKClient. Returns the test public key for any token.

    Mirrors the real client by parsing the token header first — that's where
    real PyJWKClient raises ``jwt.DecodeError`` on malformed input. Without
    this, malformed-token tests would silently pass through to ``jwt.decode``
    which raises a different (still PyJWTError) exception, and we wouldn't
    exercise the JWKS-side error path.
    """

    def __init__(self, key: Any) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        # Triggers DecodeError on malformed tokens, matching real behavior.
        jwt.get_unverified_header(token)
        return _FakeSigningKey(self._key)


class _FailingJWKSClient:
    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        raise PyJWKClientError("JWKS endpoint unreachable")


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `_get_jwks_client` with a fake that returns the test public key.

    Individual tests can override by re-patching the same attribute.
    """
    from app import dependencies

    monkeypatch.setattr(
        dependencies,
        "_get_jwks_client",
        lambda s: _FakeJWKSClient(_PUBLIC_KEY),
    )


def _make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
    }
    return Request(scope)


def _settings(api_key: str = "testkey", supabase_url: str = TEST_SUPABASE_URL) -> Settings:
    # activity_tracking off: these exercise identity extraction, not the
    # last-seen stamp (owned by test_lifecycle) — so the async auth deps don't
    # spawn a detached DB write during these unit tests.
    return Settings(
        wyrdfold_api_key=api_key,
        supabase_url=supabase_url,
        activity_tracking_enabled=False,
    )


def _mint(
    sub: str = USER_SUB,
    private_pem: bytes = _PRIVATE_PEM,
    aud: str = "authenticated",
    iss: str = TEST_ISSUER,
    exp_offset_seconds: int = 3600,
    kid: str = TEST_KID,
) -> str:
    payload = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "exp": datetime.now(UTC) + timedelta(seconds=exp_offset_seconds),
    }
    return jwt.encode(payload, private_pem, algorithm="ES256", headers={"kid": kid})


def test_api_key_matches_none_presented():
    assert _api_key_matches(None, "x") is False


def test_api_key_matches_equal():
    assert _api_key_matches("x", "x") is True


def test_api_key_matches_different():
    assert _api_key_matches("y", "x") is False


def test_api_key_matches_both_empty():
    assert _api_key_matches("", "") is False


def test_api_key_matches_same_length_different():
    assert _api_key_matches("abc", "xyz") is False


def test_verify_api_key_raises_on_missing():
    with pytest.raises(HTTPException) as exc:
        verify_api_key(key=None, s=_settings())
    assert exc.value.status_code == 401


def test_verify_api_key_raises_on_wrong():
    with pytest.raises(HTTPException) as exc:
        verify_api_key(key="wrong", s=_settings())
    assert exc.value.status_code == 401


def test_verify_api_key_returns_on_match():
    assert verify_api_key(key="testkey", s=_settings()) == "testkey"


def test_verify_supabase_jwt_unconfigured():
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings(supabase_url=""))
    assert exc.value.status_code == 503


def test_verify_supabase_jwt_missing_token():
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings())
    assert exc.value.status_code == 401


def test_verify_supabase_jwt_wrong_signature():
    bad_token = _mint(private_pem=_OTHER_PRIVATE_PEM)
    req = _make_request({"authorization": f"Bearer {bad_token}"})
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings())
    assert exc.value.status_code == 401


def test_verify_supabase_jwt_wrong_audience():
    bad_token = _mint(aud="anon")
    req = _make_request({"authorization": f"Bearer {bad_token}"})
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings())
    assert exc.value.status_code == 401


def test_verify_supabase_jwt_wrong_issuer():
    """Tokens minted by a different Supabase project (or anything not matching
    `<supabase_url>/auth/v1`) must be rejected — pinning issuer prevents a
    leaked token from another project being replayed against this one.
    """
    bad_token = _mint(iss="https://other-project.supabase.co/auth/v1")
    req = _make_request({"authorization": f"Bearer {bad_token}"})
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings())
    assert exc.value.status_code == 401


def test_verify_supabase_jwt_expired():
    token = _mint(exp_offset_seconds=-3600)
    req = _make_request({"authorization": f"Bearer {token}"})
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings())
    assert exc.value.status_code == 401


def test_verify_supabase_jwt_malformed_bearer():
    req = _make_request({"authorization": "Token abc.def.ghi"})
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings())
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "bogus_token",
    [
        "not.a.real.token",  # base64-decodes to invalid UTF-8
        "abc",  # not enough segments
        "x.y.z",  # right shape, garbage base64
    ],
    ids=["invalid-utf8-header", "not-enough-segments", "garbage-base64"],
)
def test_verify_supabase_jwt_malformed_token_returns_401(bogus_token: str):
    """Regression: PyJWKClient.get_signing_key_from_jwt parses the token
    header and raises jwt.DecodeError (a PyJWTError) on malformed input —
    NOT PyJWKClientError. Originally this leaked through as a 500 with the
    parser error in the response body. Smoke-tested 2026-05-03.
    """
    req = _make_request({"authorization": f"Bearer {bogus_token}"})
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings())
    assert exc.value.status_code == 401


def test_verify_supabase_jwt_jwks_fetch_failure(monkeypatch: pytest.MonkeyPatch):
    """If the JWKS endpoint is unreachable (or returns malformed JSON, or the
    token's `kid` isn't present after a refresh) PyJWKClient raises
    PyJWKClientError — the dep collapses it to 401 without leaking detail.
    """
    from app import dependencies

    monkeypatch.setattr(dependencies, "_get_jwks_client", lambda s: _FailingJWKSClient())
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    with pytest.raises(HTTPException) as exc:
        verify_supabase_jwt(req, s=_settings())
    assert exc.value.status_code == 401


def test_verify_supabase_jwt_valid_returns_sub():
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    assert verify_supabase_jwt(req, s=_settings()) == USER_SUB


def test_verify_api_key_or_jwt_rejects_broad_api_key():
    """#29 R3 H4 / #192: the user-data gate is JWT-only — the broad shared
    key must NOT authenticate against user routers (a leaked key can't reach
    user data)."""
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        verify_api_key_or_jwt(req, key="testkey", s=_settings())
    assert exc.value.status_code == 401


def test_verify_api_key_or_jwt_accepts_jwt():
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    assert verify_api_key_or_jwt(req, key=None, s=_settings()) == "jwt"


def test_verify_api_key_or_jwt_rejects_both_missing():
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        verify_api_key_or_jwt(req, key=None, s=_settings())
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Dedicated cron/automation key scope (#29 round 3 / H4)
# ---------------------------------------------------------------------------


def _settings_with_cron(api_key: str = "legacykey", cron_key: str = "cronkey") -> Settings:
    return Settings(
        wyrdfold_api_key=api_key,
        wyrdfold_cron_key=cron_key,
        supabase_url=TEST_SUPABASE_URL,
    )


def test_verify_api_key_accepts_cron_key_on_operator_routes():
    """The cron key authenticates the strictly-operator gate."""
    assert verify_api_key(key="cronkey", s=_settings_with_cron()) == "cronkey"


def test_verify_api_key_still_accepts_legacy_key():
    """No regression: the legacy key keeps working on operator routes."""
    assert verify_api_key(key="legacykey", s=_settings_with_cron()) == "legacykey"


def test_verify_api_key_rejects_unknown_key():
    with pytest.raises(HTTPException) as exc:
        verify_api_key(key="nope", s=_settings_with_cron())
    assert exc.value.status_code == 401


def test_cron_key_is_rejected_by_user_data_gate():
    """The key-isolation guarantee: the cron key must NOT authenticate
    against the user-data routers (verify_api_key_or_jwt). Only the legacy
    key or a JWT may. Without this, the 'narrow' cron key would be just as
    over-broad as the key it's meant to replace."""
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        verify_api_key_or_jwt(req, key="cronkey", s=_settings_with_cron())
    assert exc.value.status_code == 401


def test_legacy_key_rejected_by_user_data_gate():
    """#29 R3 H4 / #192: the broad legacy key is now rejected by the user-data
    gate too (not just the cron key). Both shared keys are barred from user
    routers; only a JWT authenticates them. Operator routes keep accepting
    both keys (see ``verify_api_key`` tests above)."""
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        verify_api_key_or_jwt(req, key="legacykey", s=_settings_with_cron())
    assert exc.value.status_code == 401


def test_cron_key_unset_changes_nothing():
    """With WYRDFOLD_CRON_KEY empty (the default), only the legacy key is
    accepted on operator routes — additive feature, off by default."""
    s = Settings(
        wyrdfold_api_key="legacykey",
        wyrdfold_cron_key="",
        supabase_url=TEST_SUPABASE_URL,
    )
    assert verify_api_key(key="legacykey", s=s) == "legacykey"
    with pytest.raises(HTTPException):
        # An empty configured cron key must never match an empty/blank
        # presented key.
        verify_api_key(key="", s=s)


async def test_get_current_user_id_returns_jwt_sub():
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    assert await get_current_user_id(req, s=_settings()) == USER_SUB


async def test_get_current_user_id_rejects_api_key_only():
    """get_current_user_id is JWT-required — api-key callers must use
    get_current_user_id_optional or a cron-only auth dep instead.
    """
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        await get_current_user_id(req, s=_settings())
    assert exc.value.status_code == 401


async def test_get_current_user_id_rejects_unauthenticated():
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        await get_current_user_id(req, s=_settings())
    assert exc.value.status_code == 401


async def test_get_current_user_id_optional_returns_jwt_sub():
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    assert await get_current_user_id_optional(req, key=None, s=_settings()) == USER_SUB


async def test_get_current_user_id_optional_returns_none_for_api_key():
    """API-key callers (cron/poller/batch) get None — services map None to
    the legacy NULL-user_id rows, preserving single-tenant behavior.
    """
    req = _make_request()
    assert await get_current_user_id_optional(req, key="testkey", s=_settings()) is None


async def test_get_current_user_id_optional_rejects_unauthenticated():
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        await get_current_user_id_optional(req, key=None, s=_settings())
    assert exc.value.status_code == 401


async def test_get_current_user_id_optional_prefers_jwt_over_api_key():
    """If both a valid JWT and a valid API key are present, prefer the JWT
    so the request runs under the user's identity, not the cron path.
    """
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    assert await get_current_user_id_optional(req, key="testkey", s=_settings()) == USER_SUB


def _budget_settings(daily: float = 5.0, hourly: float = 1.0, monthly: float = 5.0) -> Settings:
    return Settings(
        wyrdfold_api_key="testkey",
        supabase_url=TEST_SUPABASE_URL,
        user_llm_daily_budget_usd=daily,
        user_llm_hourly_budget_usd=hourly,
        user_llm_monthly_budget_usd=monthly,
    )


async def test_enforce_llm_budget_apikey_caller_bypasses(monkeypatch):
    """API-key callers (user_id=None) skip the budget check entirely — no
    supabase round-trip, no spend lookup. System paths are trusted.
    """
    from app.services.llm import budget as budget_mod

    called = False

    async def _spy(*a, **kw):
        nonlocal called
        called = True

    monkeypatch.setattr(budget_mod, "check_user_budget_async", _spy)
    await enforce_llm_budget(user_id=None, supabase=MagicMock(), s=_budget_settings())
    assert called is False


async def test_enforce_llm_budget_jwt_user_invokes_check(monkeypatch):
    from app.services.llm import budget as budget_mod

    captured: dict = {}

    async def _spy(
        supabase,
        *,
        user_id,
        daily_limit_usd,
        hourly_limit_usd,
        monthly_limit_usd,
        monthly_excluded_purposes=None,
        rail_excluded_purposes=None,
    ):
        captured.update(
            user_id=user_id,
            daily_limit_usd=daily_limit_usd,
            hourly_limit_usd=hourly_limit_usd,
            monthly_limit_usd=monthly_limit_usd,
            monthly_excluded_purposes=monthly_excluded_purposes,
            rail_excluded_purposes=rail_excluded_purposes,
        )

    monkeypatch.setattr(budget_mod, "check_user_budget_async", _spy)

    # The dep resolves the quota (tier/override/default) before the check
    # — stub it so no Supabase round-trip happens. The resolution logic
    # itself is pinned in tests/test_entitlements_tiers.py.
    async def _resolve(supabase, *, user_id):
        return budget_mod.ResolvedQuota(9.0, True, None)

    monkeypatch.setattr(budget_mod, "resolve_llm_quota_async", _resolve)
    await enforce_llm_budget(
        user_id=USER_SUB,
        supabase=MagicMock(),
        s=_budget_settings(daily=7.0, hourly=2.0, monthly=9.0),
    )
    from app.services.entitlements import NON_BILLABLE_PURPOSES

    assert captured == {
        "user_id": USER_SUB,
        "daily_limit_usd": 7.0,
        "hourly_limit_usd": 2.0,
        "monthly_limit_usd": 9.0,
        "monthly_excluded_purposes": None,
        # Rails always exclude payer-attributed background classes
        # (2026-07-13 owner-lockout fix).
        "rail_excluded_purposes": NON_BILLABLE_PURPOSES,
    }


async def test_enforce_llm_budget_passes_resolved_quota_through(monkeypatch):
    """The dep forwards whatever the resolver decided — cap AND the
    managed-tier exclusion set — without re-deriving anything. (The
    override-wins/tier logic itself is pinned in
    tests/test_entitlements_tiers.py.)"""
    from app.services.llm import budget as budget_mod

    captured: dict = {}

    async def _spy(supabase, **kw):
        captured.update(kw)

    monkeypatch.setattr(budget_mod, "check_user_budget_async", _spy)

    async def _resolve(supabase, *, user_id):
        return budget_mod.ResolvedQuota(25.0, True, ("fit.job", "poll_scoring"))

    monkeypatch.setattr(budget_mod, "resolve_llm_quota_async", _resolve)
    await enforce_llm_budget(user_id=USER_SUB, supabase=MagicMock(), s=_budget_settings(monthly=5.0))
    assert captured["monthly_limit_usd"] == 25.0
    assert captured["monthly_excluded_purposes"] == ("fit.job", "poll_scoring")
    # The hourly/daily rails must meter interactive spend only — the
    # payer-attributed background classes are excluded (2026-07-13
    # owner-lockout fix).
    from app.services.entitlements import NON_BILLABLE_PURPOSES

    assert captured["rail_excluded_purposes"] == NON_BILLABLE_PURPOSES


async def test_enforce_llm_budget_disabled_account_403s(monkeypatch):
    """The operator kill-switch blocks before any spend math runs."""
    from app.services.llm import budget as budget_mod

    async def _resolve(supabase, *, user_id):
        return budget_mod.ResolvedQuota(5.0, False, None)

    monkeypatch.setattr(budget_mod, "resolve_llm_quota_async", _resolve)
    check_spy = MagicMock()

    async def _check(*a, **kw):
        check_spy(*a, **kw)

    monkeypatch.setattr(budget_mod, "check_user_budget_async", _check)

    with pytest.raises(HTTPException) as exc:
        await enforce_llm_budget(user_id=USER_SUB, supabase=MagicMock(), s=_budget_settings())
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "llm_disabled"
    check_spy.assert_not_called()


async def test_enforce_llm_budget_propagates_429(monkeypatch):
    """If the underlying check raises 429, the dep surfaces it unchanged."""
    from app.services.llm import budget as budget_mod

    async def _resolve(supabase, *, user_id):
        return budget_mod.ResolvedQuota(9.0, True, None)

    monkeypatch.setattr(budget_mod, "resolve_llm_quota_async", _resolve)

    async def _raise(*a, **kw):
        raise HTTPException(
            status_code=429, detail={"code": "llm_budget_exceeded", "scope": "hourly"}
        )

    monkeypatch.setattr(budget_mod, "check_user_budget_async", _raise)
    with pytest.raises(HTTPException) as exc:
        await enforce_llm_budget(user_id=USER_SUB, supabase=MagicMock(), s=_budget_settings())
    assert exc.value.status_code == 429
    assert exc.value.detail["scope"] == "hourly"


# ---- get_async_supabase_for_caller (dual-auth client selection, #79 P2 / #57) --
# The sync ``get_supabase_for_caller`` was retired in #57 PR-F; these exercise the
# async replacement (same JWT-only, RLS-scoped contract), so they ``await`` and
# patch the async ``get_async_user_client``.


async def test_caller_client_jwt_returns_user_client(monkeypatch):
    """A valid JWT -> the per-request RLS-enforced async user client."""
    import app.supabase_pool as pool

    sentinel = MagicMock()

    async def _fake(token: str) -> MagicMock:
        return sentinel

    monkeypatch.setattr(pool, "get_async_user_client", _fake)
    s = Settings(
        wyrdfold_api_key="testkey",
        supabase_url=TEST_SUPABASE_URL,
        supabase_anon_key="anon",
    )
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    assert await get_async_supabase_for_caller(req, s=s) is sentinel


async def test_caller_client_jwt_without_anon_key_503():
    """Valid JWT but the user client isn't configured -> 503, never a
    silent fall-back to the service-role client (that would bypass RLS)."""
    s = Settings(
        wyrdfold_api_key="testkey",
        supabase_url=TEST_SUPABASE_URL,
        supabase_anon_key="",
    )
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    with pytest.raises(HTTPException) as exc:
        await get_async_supabase_for_caller(req, s=s)
    assert exc.value.status_code == 503


async def test_caller_client_api_key_rejected():
    """#192 defense-in-depth: an api-key caller (no bearer) gets NO client —
    401, never the service-role client. A leaked shared key can't obtain an
    RLS-bypassing client on a user route."""
    req = _make_request()  # x-api-key is no longer even read by this dep
    with pytest.raises(HTTPException) as exc:
        await get_async_supabase_for_caller(req, s=_settings())
    assert exc.value.status_code == 401


async def test_caller_client_invalid_jwt_401():
    """A bearer that fails verification 401s — there is no api-key fall-back
    anymore (the user-data gate is JWT-only)."""
    bad = _mint(private_pem=_OTHER_PRIVATE_PEM)  # wrong signature
    req = _make_request({"authorization": f"Bearer {bad}"})
    with pytest.raises(HTTPException) as exc:
        await get_async_supabase_for_caller(req, s=_settings())
    assert exc.value.status_code == 401


async def test_caller_client_no_auth_401():
    req = _make_request()
    with pytest.raises(HTTPException) as exc:
        await get_async_supabase_for_caller(req, s=_settings())
    assert exc.value.status_code == 401


# ---- Warm JWKS cache / on-loop rate-limit key_func (Perf-F1) -----------------


class _FakePyJWK:
    """PyJWK-shaped: a key id + the verifying key."""

    def __init__(self, kid: Any, key: Any) -> None:
        self.key_id = kid
        self.key = key


class _FakeJWKSet:
    def __init__(self, keys: list[Any]) -> None:
        self.keys = keys


class _JWKSClientWithSet:
    """Fake PyJWKClient exposing get_jwk_set (what refresh_jwks_cache calls)."""

    def __init__(self, keys: list[Any]) -> None:
        self._keys = keys
        self.fetches = 0

    def get_jwk_set(self, refresh: bool = False) -> _FakeJWKSet:
        self.fetches += 1
        return _FakeJWKSet(self._keys)


class _ExplodingJWKSClient:
    """Any use is a blocking fetch we must never make on the event loop."""

    def get_jwk_set(self, refresh: bool = False) -> Any:
        raise AssertionError("cache-only path must not fetch JWKS")

    def get_signing_key_from_jwt(self, token: str) -> Any:
        raise AssertionError("cache-only path must not fetch JWKS")


@pytest.fixture
def warm_cache() -> Iterator[None]:
    """Clear the module-level warm key set around each test (it's global)."""
    from app import dependencies

    dependencies._jwks_keys.clear()
    yield
    dependencies._jwks_keys.clear()


def test_refresh_jwks_cache_populates_warm_set(
    monkeypatch: pytest.MonkeyPatch, warm_cache: None
) -> None:
    from app import dependencies

    client = _JWKSClientWithSet([_FakePyJWK(TEST_KID, _PUBLIC_KEY), _FakePyJWK(None, _PUBLIC_KEY)])
    monkeypatch.setattr(dependencies, "_get_jwks_client", lambda s: client)

    n = refresh_jwks_cache(_settings())

    # The keyless entry is skipped; the kid'd one lands in the warm set.
    assert n == 1
    assert dependencies._jwks_keys[TEST_KID].key is _PUBLIC_KEY


def test_cache_only_decode_returns_sub_for_cached_kid(
    monkeypatch: pytest.MonkeyPatch, warm_cache: None
) -> None:
    from app import dependencies

    dependencies._jwks_keys[TEST_KID] = _FakeSigningKey(_PUBLIC_KEY)
    # Prove no fetch: any JWKS network use raises.
    monkeypatch.setattr(dependencies, "_get_jwks_client", lambda s: _ExplodingJWKSClient())

    req = _make_request({"authorization": f"Bearer {_mint()}"})
    assert try_decode_jwt_sub_cache_only(req, _settings()) == USER_SUB


def test_cache_only_decode_unknown_kid_returns_none_without_fetch(
    monkeypatch: pytest.MonkeyPatch, warm_cache: None
) -> None:
    from app import dependencies

    # Warm set has a DIFFERENT kid — the token's kid is unknown.
    dependencies._jwks_keys["some-other-kid"] = _FakeSigningKey(_PUBLIC_KEY)
    monkeypatch.setattr(dependencies, "_get_jwks_client", lambda s: _ExplodingJWKSClient())

    req = _make_request({"authorization": f"Bearer {_mint(kid='attacker-kid')}"})
    # None → the key_func falls back to IP keying instead of blocking on a fetch.
    assert try_decode_jwt_sub_cache_only(req, _settings()) is None


def test_cache_only_decode_cold_cache_returns_none(
    monkeypatch: pytest.MonkeyPatch, warm_cache: None
) -> None:
    from app import dependencies

    monkeypatch.setattr(dependencies, "_get_jwks_client", lambda s: _ExplodingJWKSClient())
    req = _make_request({"authorization": f"Bearer {_mint()}"})
    assert try_decode_jwt_sub_cache_only(req, _settings()) is None


def test_cache_only_decode_rejects_wrong_signature(
    monkeypatch: pytest.MonkeyPatch, warm_cache: None
) -> None:
    from app import dependencies

    dependencies._jwks_keys[TEST_KID] = _FakeSigningKey(_PUBLIC_KEY)
    monkeypatch.setattr(dependencies, "_get_jwks_client", lambda s: _ExplodingJWKSClient())

    # Signed by the other key → signature check against the cached key fails.
    req = _make_request({"authorization": f"Bearer {_mint(private_pem=_OTHER_PRIVATE_PEM)}"})
    assert try_decode_jwt_sub_cache_only(req, _settings()) is None


def test_cache_only_decode_no_bearer_returns_none(warm_cache: None) -> None:
    assert try_decode_jwt_sub_cache_only(_make_request(), _settings()) is None
