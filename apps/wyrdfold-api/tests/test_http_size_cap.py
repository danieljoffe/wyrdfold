"""Tests for get_with_size_cap — size cap + per-hop SSRF redirect gating (#110)."""

import httpx
import pytest

import app.http_client as hc


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_blocks_redirect_to_unsafe_host(monkeypatch):
    """A redirect to an internal host is rejected per-hop, before connecting."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.test":
            return httpx.Response(302, headers={"location": "http://10.0.0.5/"})
        # Must never be reached — the validator rejects 10.0.0.5 first.
        return httpx.Response(200, content=b"internal")

    client = _client(handler)
    monkeypatch.setattr(hc, "get_http_client", lambda: client)

    def validate_host(host: str) -> None:
        if host == "10.0.0.5":
            raise ValueError(f"disallowed: {host}")

    try:
        with pytest.raises(hc.UnsafeURLError):
            await hc.get_with_size_cap(
                "http://evil.test/", validate_host=validate_host
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_follows_safe_redirect(monkeypatch):
    """A redirect whose hops all pass validation is followed to the end."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "start.test":
            return httpx.Response(302, headers={"location": "http://end.test/final"})
        return httpx.Response(200, content=b"<html>final</html>")

    client = _client(handler)
    monkeypatch.setattr(hc, "get_http_client", lambda: client)

    try:
        resp, body = await hc.get_with_size_cap(
            "http://start.test/", validate_host=lambda _h: None
        )
        assert resp.status_code == 200
        assert body == b"<html>final</html>"
        assert resp.url.host == "end.test"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_size_cap_enforced(monkeypatch):
    """A body beyond the cap raises ResponseTooLargeError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    client = _client(handler)
    monkeypatch.setattr(hc, "get_http_client", lambda: client)

    try:
        with pytest.raises(hc.ResponseTooLargeError):
            await hc.get_with_size_cap(
                "http://big.test/", max_bytes=1024, validate_host=lambda _h: None
            )
    finally:
        await client.aclose()
