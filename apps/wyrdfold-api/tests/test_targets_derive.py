"""Tests for LLM-powered profile derivation (#495)."""

import json
from unittest.mock import MagicMock

import pytest

from app.models.targets import DerivedTarget
from app.services.llm.mock import MockLLMClient
from app.services.targets.derive_profile import (
    DEFAULT_MODEL,
    DEFAULT_PURPOSE,
    MIN_JD_CHARS,
    derive_profile_from_jd,
)

# A realistic JD comfortably over the MIN_JD_CHARS empty-JD guard (#47).
_VALID_JD = (
    "Senior Frontend Engineer at Acme Corp. Build React + TypeScript web apps, "
    "own the design system, and mentor engineers on the web platform team."
)


def _sample_derived_json() -> str:
    return json.dumps(
        {
            "scoring_profile": {
                "categories": {
                    "core_skills": {
                        "keywords": {"React": 3, "TypeScript": 3},
                        "weight": 2.0,
                    },
                    "secondary_skills": {
                        "keywords": {"Node.js": 2, "GraphQL": 2},
                        "weight": 1.0,
                    },
                    "nice_to_have": {
                        "keywords": {"Kubernetes": 1},
                        "weight": 0.5,
                    },
                },
                "seniority": {
                    "level": "senior",
                    "signals": ["5+ years", "lead"],
                },
                "domain": {
                    "signals": ["fintech"],
                    "weight": 0.5,
                },
                "negative": {
                    "keywords": ["junior", "intern"],
                    "weight": -10,
                },
            },
            "search_keywords": [
                "frontend engineer",
                "front-end engineer",
                "ui engineer",
            ],
        }
    )


@pytest.fixture
def llm() -> MockLLMClient:
    return MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_derived_json()})


@pytest.mark.asyncio
async def test_derive_returns_derived_target(llm: MockLLMClient):
    derived, result = await derive_profile_from_jd(
        llm, jd_text=_VALID_JD
    )
    assert isinstance(derived, DerivedTarget)
    assert derived.scoring_profile.categories["core_skills"].keywords["React"] == 3
    assert derived.scoring_profile.seniority.level == "senior"
    assert "fintech" in derived.scoring_profile.domain.signals
    assert "junior" in derived.scoring_profile.negative.keywords
    assert "frontend engineer" in derived.search_keywords


@pytest.mark.asyncio
async def test_derive_uses_default_model_and_purpose(llm: MockLLMClient):
    await derive_profile_from_jd(llm, jd_text=_VALID_JD)
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == DEFAULT_MODEL
    assert llm.calls[0]["purpose"] == DEFAULT_PURPOSE


@pytest.mark.asyncio
async def test_derive_enables_system_cache(llm: MockLLMClient):
    await derive_profile_from_jd(llm, jd_text=_VALID_JD)
    assert llm.calls[0]["cache_system"] is True


@pytest.mark.asyncio
async def test_derive_sends_jd_as_user_message(llm: MockLLMClient):
    jd = _VALID_JD
    await derive_profile_from_jd(llm, jd_text=jd)
    assert llm.calls[0]["messages_count"] == 1


@pytest.mark.asyncio
async def test_derive_returns_result_with_cost(llm: MockLLMClient):
    _, result = await derive_profile_from_jd(llm, jd_text=_VALID_JD)
    assert result.cost_usd > 0
    assert result.latency_ms > 0


@pytest.mark.asyncio
async def test_derive_model_override():
    client = MockLLMClient(
        scripted={"custom.purpose": _sample_derived_json()}
    )
    derived, _ = await derive_profile_from_jd(
        client,
        jd_text=_VALID_JD,
        model="claude-haiku-4-5",
        purpose="custom.purpose",
    )
    assert isinstance(derived, DerivedTarget)
    assert client.calls[0]["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_derive_invalid_json_raises():
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: "not valid json"})
    with pytest.raises(Exception):
        await derive_profile_from_jd(client, jd_text=_VALID_JD)


@pytest.mark.asyncio
@pytest.mark.parametrize("jd", ["", "   \n\t  ", "404 Not Found", "x" * (MIN_JD_CHARS - 1)])
async def test_derive_rejects_short_jd_without_calling_llm(jd: str) -> None:
    # #47: an empty/garbage JD must never reach the LLM — it would hallucinate a
    # profile from nothing.
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_derived_json()})
    with pytest.raises(ValueError, match="too short"):
        await derive_profile_from_jd(client, jd_text=jd)
    assert client.calls == []  # no LLM call


@pytest.mark.asyncio
async def test_derive_short_jd_never_touches_the_cache() -> None:
    # The guard runs before the cache lookup AND the cache write, so a junk JD
    # can't poison the shared target's content-hash cache (#47).
    client = MockLLMClient(scripted={DEFAULT_PURPOSE: _sample_derived_json()})
    supabase = MagicMock()
    with pytest.raises(ValueError, match="too short"):
        await derive_profile_from_jd(client, jd_text="too short", supabase=supabase)
    supabase.table.assert_not_called()  # no cache read or write
    assert client.calls == []
