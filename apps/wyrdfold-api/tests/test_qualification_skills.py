"""Catalog-wide skill extraction (backs /search?skill=react).

Pins the contracts that would silently break the facet or the poll cycle:

- normalization/dedupe/cap agreement with the search filter's vocabulary (the
  DB predicate is exact-string containment — a casing split returns zero);
- fail-soft: a malformed or absent response yields ``[]``, never an exception
  into the poll cycle;
- the poller wiring: skills ride the tag write when present, are OMITTED when
  empty (so a later richer read isn't blanked), and a skills failure never
  costs the tags that were already computed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings as live_settings
from app.services import poller as poller_mod
from app.services.llm.errors import LLMQuotaExhaustedError
from app.services.llm.mock import MockLLMClient
from app.services.qualification import QualificationTags, extract_skills
from app.services.qualification.skills import MAX_SKILLS, ExtractedSkills

_JD = "We need a senior engineer. Requirements: React, TypeScript, Node.js, Postgres."


# ---- schema / normalization --------------------------------------------------


def test_normalizes_dedupes_and_caps() -> None:
    """One vocabulary across writer and reader. ``normalize_skill`` is shared
    with the Phase-2 harvest and ``job_search.normalize_skill_filter``."""
    parsed = ExtractedSkills.model_validate(
        {
            "skills": [
                "React",
                "react",  # dupe after normalization
                "  TypeScript  ",
                "Kubernetes — named in the platform section",  # evidence clause
                42,  # non-string
                "x" * 61,  # phrase-length
                "node.js",
                "sql",
                "figma",
                "terraform",
                "aws",
                "docker",  # past the cap
            ]
        }
    )
    assert parsed.skills.count("react") == 1
    assert "typescript" in parsed.skills
    assert "kubernetes" in parsed.skills  # clause stripped
    assert len(parsed.skills) == MAX_SKILLS
    assert all(s == s.lower() and len(s) <= 60 for s in parsed.skills)


@pytest.mark.parametrize("bad", ["react, node", {"a": 1}, 7, None])
def test_malformed_skills_degrade_to_empty(bad: object) -> None:
    assert ExtractedSkills.model_validate({"skills": bad}).skills == []


def test_missing_field_is_valid_and_empty() -> None:
    assert ExtractedSkills.model_validate({}).skills == []


# ---- extract_skills ---------------------------------------------------------


@pytest.mark.asyncio
async def test_extracts_through_the_real_parse_path() -> None:
    client = MockLLMClient(
        scripted={"qualification.skills": '{"skills": ["React", "TypeScript", "node.js"]}'}
    )
    skills, result = await extract_skills(client, title="Senior FE", description=_JD)
    assert skills == ["react", "typescript", "node.js"]
    assert result is not None  # cost is loggable


@pytest.mark.asyncio
async def test_empty_description_spends_nothing() -> None:
    """No JD means nothing to read — never pay for a call that can't succeed."""
    client = MockLLMClient(scripted={"qualification.skills": '{"skills": ["react"]}'})
    for desc in (None, "", "   "):
        skills, result = await extract_skills(client, title="T", description=desc)
        assert skills == []
        assert result is None


@pytest.mark.asyncio
async def test_garbage_response_fails_soft() -> None:
    """A non-JSON body leaves the column NULL rather than raising into the
    poll cycle (the tagger's fail-soft contract)."""
    client = MockLLMClient(scripted={"qualification.skills": "I cannot help with that."})
    skills, result = await extract_skills(client, title="T", description=_JD)
    assert skills == []
    assert result is None


@pytest.mark.asyncio
async def test_provider_fatal_propagates_for_the_breaker() -> None:
    """A dead key / spent cap must reach the caller so the poller can latch its
    breaker — swallowing it would let the cycle hammer a dead provider."""
    # ``complete_json`` drives ``complete_tool_use`` (the forced-tool path), so
    # that is the method to fail — stubbing ``complete`` would raise a
    # MagicMock-shaped error instead and pass through the generic handler,
    # making this test assert nothing about the breaker contract.
    client = MagicMock()
    client.complete_tool_use = AsyncMock(side_effect=LLMQuotaExhaustedError("402"))
    with pytest.raises(LLMQuotaExhaustedError):
        await extract_skills(client, title="T", description=_JD)


# ---- poller wiring ----------------------------------------------------------

_TAGS = QualificationTags(
    is_us=True,
    us_confidence=98,
    role_family="engineering",
    seniority="senior_ic",
    employment_type="full_time",
    metro="San Francisco",
    is_remote=False,
    is_genuine_role=True,
)


def _row() -> dict[str, Any]:
    return {
        "id": "job-1",
        "title": "Staff Engineer",
        "company_name": "Acme",
        "location": "San Francisco, CA",
        "description_html": f"<p>{_JD}</p>",
    }


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    skills_result: object,
) -> dict[str, Any]:
    """Patch the poller's tagger/clients/write, with ``extract_skills`` scripted
    to either return a value or raise. Returns a recorder."""
    rec: dict[str, Any] = {"writes": [], "costs": []}

    async def fake_get_client(_sb: object, user_id: str | None) -> object:
        return MagicMock()

    async def fake_tag_job(_llm: object, **_kw: Any) -> Any:
        return (_TAGS, object())

    async def fake_extract(_llm: object, **_kw: Any) -> Any:
        if isinstance(skills_result, Exception):
            raise skills_result
        return skills_result

    monkeypatch.setattr(poller_mod, "get_llm_client_async", fake_get_client)
    monkeypatch.setattr(poller_mod, "tag_job", fake_tag_job)
    monkeypatch.setattr(poller_mod, "extract_skills", fake_extract)
    monkeypatch.setattr(
        poller_mod,
        "enqueue_llm_cost",
        lambda uid, purpose, res: rec["costs"].append(purpose),
    )
    meter = MagicMock()
    meter.rpc.return_value.execute = AsyncMock(return_value=MagicMock(data=1.0))
    monkeypatch.setattr(poller_mod, "_async_service_client", lambda: meter)
    monkeypatch.setattr(live_settings, "skills_extraction_enabled", True)
    return rec


def _supabase(rec: dict[str, Any]) -> MagicMock:
    sb = MagicMock()

    def update(payload: dict[str, Any]) -> MagicMock:
        rec["writes"].append(payload)
        chain = MagicMock()
        chain.eq.return_value.execute = MagicMock()
        return chain

    sb.table.return_value.update.side_effect = update
    return sb


@pytest.mark.asyncio
async def test_skills_ride_the_tag_write(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, skills_result=(["react", "typescript"], object()))
    await poller_mod._qualify_jobs(_supabase(rec), [_row()])

    payload = rec["writes"][0]
    assert payload["skills_required"] == ["react", "typescript"]
    assert payload["role_family"] == "engineering"  # tags unaffected
    # ONE write for both, and both spends are logged under their own purposes.
    assert len(rec["writes"]) == 1
    assert rec["costs"] == ["qualification.tagger", "qualification.skills"]


@pytest.mark.asyncio
async def test_empty_skills_omit_the_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never write an empty list: the Phase-2 harvest writes the same column
    from a full-JD read, and a blanking write would erase its richer value."""
    rec = _wire(monkeypatch, skills_result=([], None))
    await poller_mod._qualify_jobs(_supabase(rec), [_row()])

    assert "skills_required" not in rec["writes"][0]
    assert rec["writes"][0]["role_family"] == "engineering"


@pytest.mark.asyncio
async def test_skills_failure_never_costs_the_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider-fatal skills call latches the breaker but must NOT lose the
    tags already computed — losing classification to an enrichment failure
    would be strictly worse than a NULL column."""
    rec = _wire(monkeypatch, skills_result=LLMQuotaExhaustedError("402"))
    monkeypatch.setattr(poller_mod, "_trip_provider_fatal", lambda _e: None)

    await poller_mod._qualify_jobs(_supabase(rec), [_row()])

    assert len(rec["writes"]) == 1
    assert rec["writes"][0]["role_family"] == "engineering"
    assert "skills_required" not in rec["writes"][0]


@pytest.mark.asyncio
async def test_flag_off_skips_extraction_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}

    async def _never(*_a: object, **_k: object) -> Any:
        called["n"] += 1
        return (["react"], object())

    rec = _wire(monkeypatch, skills_result=(["react"], object()))
    monkeypatch.setattr(poller_mod, "extract_skills", _never)
    monkeypatch.setattr(live_settings, "skills_extraction_enabled", False)

    await poller_mod._qualify_jobs(_supabase(rec), [_row()])

    assert called["n"] == 0
    assert "skills_required" not in rec["writes"][0]
    assert rec["costs"] == ["qualification.tagger"]
