from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.http_client import BoardFetchError
from app.services.lever import fetch_lever_jobs
from app.services.standard_job import StandardJob


def _mock_response(status_code: int, json_data: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else []
    if status_code >= 400 and status_code != 404:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# A board that doesn't answer with a listing RAISES; a board that answers with
# an empty listing returns []. Collapsing both into [] is what let a dead board
# reset its own failure counter forever — see
# tests/test_dead_board_failure_accounting.py.


@pytest.mark.asyncio
async def test_fetch_404_raises(mock_http_client):
    resp = _mock_response(404)
    mock_http_client.get = AsyncMock(return_value=resp)
    with pytest.raises(BoardFetchError) as exc_info:
        await fetch_lever_jobs("missing")
    assert exc_info.value.status == 404


@pytest.mark.asyncio
async def test_fetch_http_error_raises(mock_http_client):
    mock_http_client.get = AsyncMock(side_effect=httpx.HTTPError("boom"))
    with pytest.raises(BoardFetchError):
        await fetch_lever_jobs("bad")


@pytest.mark.asyncio
async def test_empty_board_returns_empty_list(mock_http_client):
    """CONTROL: Lever answers with a bare array — an empty one is a real,
    healthy board with nothing open."""
    mock_http_client.get = AsyncMock(return_value=_mock_response(200, []))
    assert await fetch_lever_jobs("quiet") == []


@pytest.mark.asyncio
async def test_fetch_valid_json_maps_jobs(mock_http_client):
    payload = [
        {
            "id": "abc-123",
            "text": "Senior Frontend Engineer",
            "categories": {"location": "Remote", "team": "Engineering"},
            "description": "<p>desc</p>",
            "createdAt": 1700000000000,
            "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        }
    ]
    resp = _mock_response(200, payload)
    mock_http_client.get = AsyncMock(return_value=resp)
    result = await fetch_lever_jobs("acme")
    assert len(result) == 1
    job = result[0]
    assert isinstance(job, StandardJob)
    assert job.external_id == "abc-123"
    assert job.title == "Senior Frontend Engineer"
    assert job.location_name == "Remote"
    assert job.absolute_url == "https://jobs.lever.co/acme/abc-123"


@pytest.mark.asyncio
async def test_fetch_non_list_response_raises(mock_http_client):
    """A 200 whose body isn't the expected array is a malformed response, not
    a board with nothing open — it must not look like a clean poll."""
    resp = _mock_response(200, {"error": "not found"})
    mock_http_client.get = AsyncMock(return_value=resp)
    with pytest.raises(BoardFetchError):
        await fetch_lever_jobs("bad")


@pytest.mark.asyncio
async def test_fetch_missing_categories(mock_http_client):
    payload = [
        {
            "id": "x",
            "text": "Engineer",
            "categories": {},
            "description": "",
            "createdAt": 0,
            "hostedUrl": "",
        }
    ]
    resp = _mock_response(200, payload)
    mock_http_client.get = AsyncMock(return_value=resp)
    result = await fetch_lever_jobs("co")
    assert result[0].location_name is None
