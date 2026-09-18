"""Release-gate probes for the 2026-09-18 release (#1072 + #1073 + #1074 + #1075).

Cross-PR interaction probes driven through the REAL FastAPI app and its
registered handlers, not through the service functions alone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies import (
    enforce_llm_budget,
    get_async_service_supabase,
    get_current_user_id,
    get_llm_client,
    verify_api_key_or_jwt,
)
from app.main import app
from app.models.experience import OptimizedPayload
from app.services.extract import ExtractionResult
from app.services.llm.errors import LLMMalformedOutputError
from app.services.llm.mock import MockLLMClient
from app.services.targets import from_input
from app.services.validate import ValidationResult

RAW_TITLE = "Senior Product Builder (Product Manager), Enterprise Readiness & Admin Platform"
MARKER = "Enterprise Readiness"


@pytest.fixture
def _from_url_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    from app.routers import targets as mod

    monkeypatch.setattr(
        mod,
        "_optimized_latest",
        AsyncMock(return_value=SimpleNamespace(payload=OptimizedPayload())),
    )
    monkeypatch.setattr(
        mod,
        "validate_job_url",
        AsyncMock(
            return_value=ValidationResult(is_valid=True, final_url="https://example.com/jobs/1")
        ),
    )
    monkeypatch.setattr(
        mod,
        "_fetch_jd_from_url",
        AsyncMock(
            return_value=ExtractionResult(
                title=RAW_TITLE, description_html="x" * 400, company_name="Acme"
            )
        ),
    )
    matcher = AsyncMock(side_effect=AssertionError("must not match on a failed normalization"))
    creator = AsyncMock(side_effect=AssertionError("must not create on a failed normalization"))
    monkeypatch.setattr(from_input, "find_matching_target", matcher)
    monkeypatch.setattr(from_input, "_create_and_link", creator)
    return {"matcher": matcher, "creator": creator}


def _client(llm: MockLLMClient) -> TestClient:
    app.dependency_overrides[get_async_service_supabase] = lambda: object()
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "u1"
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[enforce_llm_budget] = lambda: None
    return TestClient(app)


def test_from_url_blank_label_is_a_fixed_502_and_never_reaches_matching_or_creation(
    _from_url_seams: dict[str, AsyncMock],
) -> None:
    """#1072 through the HTTP layer: the normalizer returns a whitespace-only
    label (the case the old fallback laundered into a raw-title identity).
    The response is the fixed 502; the raw title never reaches the matcher,
    the create, or the body."""
    llm = MockLLMClient(scripted={"target.normalize_posting_title": json.dumps({"label": "   "})})
    resp = _client(llm).post("/targets/from-url", json={"jd_url": "https://example.com/jobs/1"})
    assert resp.status_code == 502
    assert resp.json() == {
        "detail": LLMMalformedOutputError.user_message,
        "code": "schema_violation",
    }
    assert MARKER not in resp.text
    assert "label" not in resp.text.lower()
    _from_url_seams["matcher"].assert_not_called()
    _from_url_seams["creator"].assert_not_called()


def test_from_url_prose_refusal_is_a_fixed_502_without_model_content(
    _from_url_seams: dict[str, AsyncMock],
) -> None:
    llm = MockLLMClient(
        scripted={"target.normalize_posting_title": f"Sure! ZZ_MODEL_TEXT_ZZ {RAW_TITLE}"}
    )
    resp = _client(llm).post("/targets/from-url", json={"jd_url": "https://example.com/jobs/1"})
    assert resp.status_code == 502
    assert resp.json()["code"] == "missing_tool_call"
    assert "ZZ_MODEL_TEXT_ZZ" not in resp.text
    assert MARKER not in resp.text
    _from_url_seams["matcher"].assert_not_called()
    _from_url_seams["creator"].assert_not_called()


def test_from_url_canonical_label_reaches_the_create_through_the_http_layer(
    _from_url_seams: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path, end to end: the create receives the stripped canonical
    label, never the posting title."""
    from tests.test_targets_from_input import _target, _user_target

    seen: dict[str, str] = {}

    async def _match(_s, label):  # type: ignore[no-untyped-def]
        seen["matched"] = label
        return None

    async def _create(_s, *, user_id, payload, activation_status=None):  # type: ignore[no-untyped-def]
        seen["created"] = payload.label
        target = _target(id="new", label=payload.label)
        return target, _user_target(target_id=target.id)

    monkeypatch.setattr(from_input, "find_matching_target", _match)
    monkeypatch.setattr(from_input, "_create_and_link", _create)
    monkeypatch.setattr(from_input, "spawn_detached", lambda *a, **k: None)
    monkeypatch.setattr(from_input.cost_log, "record_async", AsyncMock(return_value=None))
    llm = MockLLMClient(
        scripted={
            "target.normalize_posting_title": json.dumps({"label": "  Senior Product Manager  "})
        }
    )
    resp = _client(llm).post("/targets/from-url", json={"jd_url": "https://example.com/jobs/1"})
    assert resp.status_code == 201, resp.text
    assert seen == {"matched": "Senior Product Manager", "created": "Senior Product Manager"}
    assert MARKER not in json.dumps(seen)


# ---- #1072 x #1075: an error AFTER the SSE response opened ------------------


def test_derive_stream_mid_stream_error_frame_is_a_fixed_sse_error_without_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw transport (#1075) maps a mid-stream ``event: error`` onto the
    typed hierarchy (#1072); the derive route cannot turn that into a JSON 502
    because headers are already sent. It must emit the fixed ``error`` SSE
    frame, no ``done`` frame, and none of the provider's text."""
    from app.dependencies import get_async_user_supabase, get_embeddings_client
    from app.routers import experience as mod
    from app.services.experience import derive
    from app.services.llm import raw_messages_transport as raw
    from app.services.llm.openrouter_client import OpenRouterLLMClient
    from tests.support.messages_wire import mock_http, sse_frames, sse_response

    body = sse_frames(
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
        (
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}},
        ),
        (
            "error",
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "ZZ_VENDOR_TEXT_ZZ"},
            },
        ),
    )
    resp, stream = sse_response(body)
    client = OpenRouterLLMClient(api_key="k", raw_purposes=frozenset({derive.DEFAULT_PURPOSE}))
    client._raw = raw.RawMessagesTransport(
        api_key="k",
        timeout=5.0,
        max_retries=0,
        base_url="https://openrouter.ai/api",
        http=mock_http(resp),
    )

    monkeypatch.setattr(
        mod,
        "_prose_latest",
        AsyncMock(return_value=SimpleNamespace(id="prose-1", content="my resume prose")),
    )
    monkeypatch.setattr(mod, "_optimized_latest", AsyncMock(return_value=None))
    app.dependency_overrides[get_async_user_supabase] = lambda: object()
    app.dependency_overrides[get_async_service_supabase] = lambda: object()
    app.dependency_overrides[get_embeddings_client] = lambda: object()
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "u1"
    app.dependency_overrides[get_llm_client] = lambda: client
    app.dependency_overrides[enforce_llm_budget] = lambda: None

    with TestClient(app).stream("POST", "/experience/derive/stream") as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())
    frames = [f for f in text.split("\n\n") if f.strip()]
    events = [f.split("\n")[0] for f in frames]
    assert events[0] == "event: delta"
    assert events[-1] == "event: error"
    assert "event: done" not in events
    assert "the derive stream failed; please retry" in frames[-1]
    assert "ZZ_VENDOR_TEXT_ZZ" not in text
    assert "invalid_request" not in text
    assert stream.closed  # the upstream socket was released by the transport's finally
