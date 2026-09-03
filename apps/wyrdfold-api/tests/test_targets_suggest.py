"""Deterministic LLM tests for target suggestion (#495 / audit F0-E).

Asserts that suggest_targets correctly parses scripted LLM JSON, wires the
right model/purpose/cache flags, and surfaces parse errors instead of
silently corrupting downstream callers.
"""

import json

import pytest

from app.models.experience import OptimizedPayload, Role, Skill
from app.models.targets import TargetSuggestions
from app.services.llm.mock import MockLLMClient
from app.services.targets.suggest import (
    DEFAULT_MODEL,
    DEFAULT_PURPOSE,
    suggest_targets,
)


def _payload() -> OptimizedPayload:
    return OptimizedPayload(
        summary="Senior frontend engineer with 8 years building React apps.",
        roles=[
            Role(
                id="r1",
                company="Acme",
                title="Senior Frontend Engineer",
                start="2020",
                end="present",
                skills=["React", "TypeScript"],
            ),
        ],
        skills=[
            Skill(name="React", years=8.0),
            Skill(name="TypeScript", years=6.0),
        ],
    )


def _scripted_response() -> str:
    return json.dumps(
        {
            "suggestions": [
                {
                    "label": "Staff Frontend Engineer",
                    "description": "Senior IC roles emphasizing systems and DX.",
                    "core_skills": ["React", "TypeScript", "Testing"],
                },
                {
                    "label": "Engineering Manager",
                    "description": "Player-coach roles leveraging your IC depth.",
                    "core_skills": ["React", "Mentorship", "Architecture"],
                },
            ]
        }
    )


@pytest.fixture
def llm() -> MockLLMClient:
    return MockLLMClient(scripted={DEFAULT_PURPOSE: _scripted_response()})


@pytest.mark.asyncio
async def test_suggest_returns_target_suggestions(llm: MockLLMClient) -> None:
    suggestions, _ = await suggest_targets(llm, payload=_payload())
    assert isinstance(suggestions, TargetSuggestions)
    assert len(suggestions.suggestions) == 2
    assert suggestions.suggestions[0].label == "Staff Frontend Engineer"
    assert "React" in suggestions.suggestions[0].core_skills


@pytest.mark.asyncio
async def test_suggest_uses_default_model_and_purpose(llm: MockLLMClient) -> None:
    await suggest_targets(llm, payload=_payload())
    assert llm.calls[0]["model"] == DEFAULT_MODEL
    assert llm.calls[0]["purpose"] == DEFAULT_PURPOSE


@pytest.mark.asyncio
async def test_suggest_caches_system_prompt(llm: MockLLMClient) -> None:
    await suggest_targets(llm, payload=_payload())
    assert llm.calls[0]["cache_system"] is True


@pytest.mark.asyncio
async def test_suggest_handles_empty_payload(llm: MockLLMClient) -> None:
    suggestions, _ = await suggest_targets(llm, payload=OptimizedPayload())
    assert isinstance(suggestions, TargetSuggestions)


@pytest.mark.asyncio
async def test_suggest_invalid_json_raises() -> None:
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: "not json at all"})
    with pytest.raises(Exception):
        await suggest_targets(client, payload=_payload())


# ---------------------------------------------------------------------------
# #864 — the endpoint ships the caller's active-target allowance
# ---------------------------------------------------------------------------


def test_suggest_endpoint_ships_the_active_target_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The onboarding wizard pre-selected every suggestion and offered
    "Create 3 targets" on a 2-target plan (#864). The offer can only fit the
    plan if the plan travels with the suggestions — cap/active/remaining ride
    the response."""
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from app.dependencies import (
        enforce_llm_budget,
        get_async_service_supabase,
        get_current_user_id,
        get_llm_client,
        verify_api_key_or_jwt,
    )
    from app.main import app
    from app.models.targets import MatchedSuggestions
    from app.routers import targets as router_mod

    doc = MagicMock()
    doc.payload = _payload()
    monkeypatch.setattr(router_mod, "_optimized_latest", AsyncMock(return_value=doc))
    monkeypatch.setattr(
        router_mod,
        "suggest_and_match",
        AsyncMock(return_value=(MatchedSuggestions(matches=[]), MagicMock())),
    )
    monkeypatch.setattr(router_mod.cost_log, "record_async", AsyncMock())
    monkeypatch.setattr(router_mod, "_count_active_for_user_async", AsyncMock(return_value=1))
    monkeypatch.setattr(router_mod, "_effective_active_target_cap_async", AsyncMock(return_value=2))

    app.dependency_overrides[verify_api_key_or_jwt] = lambda: "jwt"
    app.dependency_overrides[get_async_service_supabase] = lambda: MagicMock()
    app.dependency_overrides[get_current_user_id] = lambda: "user-1"
    app.dependency_overrides[get_llm_client] = lambda: MagicMock()
    app.dependency_overrides[enforce_llm_budget] = lambda: None
    try:
        resp = TestClient(app).post("/targets/suggest")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["allowance"] == {"cap": 2, "active": 1, "remaining": 1}
