"""``/version`` — which build is actually serving traffic.

Exists because release #794 could not be verified. The usual discriminator is a
schema diff in ``/openapi.json``, but a BEHAVIOURAL release changes no schema,
Railway auth was expired, and no response header carries a deploy id. There was
no way to tell the new build from the old one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers={"host": "localhost"})


def test_version_is_public(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """No token. A probe you need credentials for is useless in an incident."""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc1234")
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json()["commit"] == "abc1234"


def test_build_sha_is_the_portable_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setenv("BUILD_SHA", "deadbee")
    assert client.get("/version").json()["commit"] == "deadbee"


def test_railway_wins_over_the_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "from-railway")
    monkeypatch.setenv("BUILD_SHA", "stale-local")
    assert client.get("/version").json()["commit"] == "from-railway"


def test_missing_sha_reports_null_rather_than_lying(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown build must read as unknown — a fabricated or stale value
    would be worse than no endpoint at all, since the whole point is trust."""
    for var in ("RAILWAY_GIT_COMMIT_SHA", "BUILD_SHA", "BUILD_TIME"):
        monkeypatch.delenv(var, raising=False)
    body = client.get("/version").json()
    assert body["commit"] is None
    assert body["built_at"] is None


def test_leaks_no_configuration(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only build identity — never config or secrets."""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc1234")
    assert set(client.get("/version").json()) == {"commit", "built_at", "environment"}
