"""Wire-level doubles for OpenRouter's ``/api/v1/messages`` (#1067 PR B).

Built FROM the recorded corpus in ``tests/fixtures/openrouter_messages/``, not
from the SDK's object model: the raw transport never sees SDK objects, so
these are the only doubles that can catch a parser drift against the real
wire. Cost numbers in the corpus are placeholders (see its README).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "openrouter_messages"
MESSAGES_URL = "https://openrouter.ai/api/v1/messages"


def fixture(name: str) -> dict[str, Any]:
    """A recorded exchange: ``{"status", "headers", "request", "body"}``."""
    return json.loads((FIXTURES / name).read_text())


def wire_tool_use(
    tool_input: dict[str, Any],
    *,
    tool_name: str = "return_X",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read: int = 0,
    cache_creation: int = 0,
    provider: str | None = "Amazon Bedrock",
    stop_reason: str = "tool_use",
) -> dict[str, Any]:
    """A 2xx body shaped exactly like ``tool_use_with_defs.json``."""
    body = fixture("tool_use_with_defs.json")["body"]
    body["content"] = [
        {
            "type": "tool_use",
            "id": "toolu_test",
            "caller": {"type": "direct"},
            "name": tool_name,
            "input": tool_input,
        }
    ]
    body["stop_reason"] = stop_reason
    body["usage"]["input_tokens"] = input_tokens
    body["usage"]["output_tokens"] = output_tokens
    body["usage"]["cache_read_input_tokens"] = cache_read
    body["usage"]["cache_creation_input_tokens"] = cache_creation
    if provider is None:
        body.pop("provider", None)
    else:
        body["provider"] = provider
    return body


def wire_text(text: str, *, stop_reason: str = "end_turn") -> dict[str, Any]:
    body = fixture("text_complete.json")["body"]
    body["content"] = [{"type": "text", "text": text, "citations": []}]
    body["stop_reason"] = stop_reason
    return body


def wire_error(
    status: int, message: str, *, error_type: str = "invalid_request_error"
) -> dict[str, Any]:
    """The Anthropic-shaped error envelope OpenRouter sends with a real 4xx."""
    return {
        "type": "error",
        "error": {"type": error_type, "message": message, "error_type": "invalid_request"},
        "request_id": "gen-test",
    }


class WireCapture:
    """Records every request the transport sends: ``(headers, json body)``."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []


def mock_http(
    *steps: httpx.Response | Exception | Callable[[httpx.Request], httpx.Response],
    capture: WireCapture | None = None,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` over ``MockTransport`` that answers successive
    requests with ``steps`` in order (a Response, an Exception to raise, or a
    callable), recording each request into ``capture``."""
    queue = list(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.requests.append(
                ({k.lower(): v for k, v in request.headers.items()}, json.loads(request.content))
            )
        if not queue:
            raise AssertionError("wire double exhausted: more requests than scripted steps")
        step = queue.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(request)
        return step

    # No default headers on purpose: auth and version are the transport's job
    # on every request, and a double that supplied them would hide their loss.
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_response(
    status: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})
