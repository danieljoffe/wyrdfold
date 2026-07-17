"""The LLM edge battery — every complete_json surface × the edge corpus.

Runs the named edges from ``tests/support/llm_edges.py`` against all three
tool-use parse surfaces (derive → OptimizedPayload, analysis → JobAnalysis,
tailor → TailoredResume) plus the one raw-text parse surface (the
``derive/stream`` inline ``strip_markdown_fence`` + ``model_validate_json``).

Grow this file alongside the corpus (CLAUDE.md): a new LLM surface gets a
``Surface`` entry and inherits the whole battery; a new LLM bug gets a named
corpus behavior + a case here.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.analysis import JobAnalysis, Scorecard, SkillMatch
from app.models.experience import OptimizedPayload, Outcome, Role, Skill
from app.models.llm import Message
from app.models.tailor import (
    ContactInfo,
    TailoredBullet,
    TailoredEducation,
    TailoredResume,
    TailoredRole,
)
from app.models.targets import TargetSuggestion, TargetSuggestions
from app.services.analysis.analyze import analyze_job
from app.services.experience.derive import derive_from_prose
from app.services.llm.client import strip_markdown_fence
from app.services.llm.mock import MockLLMClient
from app.services.tailor.tailor import tailor_resume
from app.services.targets.suggest import suggest_targets_from_query
from tests.support.llm_edges import (
    INJECTION_TEXT,
    TEXT_EDGES,
    UNICODE_TEXT,
    fenced,
    inject_into_strings,
    schema_violations,
    truncate_json,
)

# ---------------------------------------------------------------------------
# Valid baseline payloads (minimal, self-contained)
# ---------------------------------------------------------------------------


def _optimized() -> OptimizedPayload:
    return OptimizedPayload(
        summary="Senior frontend with a focus on performance.",
        roles=[
            Role(
                id="fc",
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                end="2024-04",
                summary="Led the PDP rebuild and cut mobile load times.",
                skills=["React", "Next.js"],
                outcome_refs=[],
            )
        ],
        skills=[Skill(name="React"), Skill(name="TypeScript")],
        outcomes=[
            Outcome(
                description="Cut mobile load times from 10s to 2s",
                metric="LCP",
                value="2s",
                role_ref="fc",
            )
        ],
    )


def _valid_optimized_dict() -> dict[str, Any]:
    return _optimized().model_dump(mode="json")


def _valid_analysis_dict() -> dict[str, Any]:
    return JobAnalysis(
        scorecard=Scorecard(
            skills_matched=[
                SkillMatch(
                    name="React", matched=True, confidence="high", evidence="skills list"
                )
            ],
            skills_missing=["GraphQL"],
            nice_to_haves=[],
            seniority_fit="strong",
            seniority_rationale="Years match.",
            domain_fit="moderate",
            domain_rationale="Adjacent domain.",
        ),
        recommendation="Apply: strong technical match.",
    ).model_dump(mode="json")


def _valid_suggest_from_query_dict() -> dict[str, Any]:
    return TargetSuggestions(
        suggestions=[
            TargetSuggestion(
                label="Senior Frontend Engineer",
                description="Frontend roles leveraging React/TypeScript.",
                core_skills=["React", "TypeScript"],
            )
        ]
    ).model_dump(mode="json")


def _valid_resume_dict() -> dict[str, Any]:
    return TailoredResume(
        summary="Senior FE with 10 years of shipped work.",
        contact=ContactInfo(name="Test User", email="test@example.com"),
        experience=[
            TailoredRole(
                company="FightCamp",
                title="Senior Frontend Engineer",
                start="2021-11",
                end="2024-04",
                bullets=[
                    TailoredBullet(
                        text="Cut mobile load times from 10s to 2s.",
                        source_outcome_ref="Cut mobile load times from 10s to 2s",
                    )
                ],
                source_role_ref="fc",
            )
        ],
        skills=["React", "TypeScript"],
        education=[TailoredEducation(school="UCLA", degree="BA")],
        jd_snippet="Senior FE role",
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# The surfaces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Surface:
    name: str
    purpose: str
    schema: type
    valid: Callable[[], dict[str, Any]]
    call: Callable[[MockLLMClient], Awaitable[Any]]
    # (parsed-result) -> the string field the injection/unicode probes check
    text_field: Callable[[Any], str | None]


SURFACES = [
    Surface(
        name="derive",
        purpose="experience.derive",
        schema=OptimizedPayload,
        valid=_valid_optimized_dict,
        call=lambda llm: derive_from_prose(llm, prose_text="Ten years of frontend."),
        text_field=lambda res: res[0].summary,
    ),
    Surface(
        name="analysis",
        purpose="job_analysis",
        schema=JobAnalysis,
        valid=_valid_analysis_dict,
        call=lambda llm: analyze_job(
            llm, optimized=_optimized(), job_description="We want a senior FE."
        ),
        text_field=lambda res: res[0].recommendation,
    ),
    Surface(
        name="tailor",
        purpose="tailor.resume",
        schema=TailoredResume,
        valid=_valid_resume_dict,
        call=lambda llm: tailor_resume(
            llm,
            optimized=_optimized(),
            job_description="We want a senior FE.",
            contact=ContactInfo(name="Test User", email="test@example.com"),
        ),
        text_field=lambda res: res[0].summary,
    ),
    Surface(
        name="suggest_from_query",
        purpose="target.suggest_from_query",
        schema=TargetSuggestions,
        valid=_valid_suggest_from_query_dict,
        call=lambda llm: suggest_targets_from_query(
            llm, query="senior frontend engineer"
        ),
        text_field=lambda res: (
            res[0].suggestions[0].description if res[0].suggestions else None
        ),
    ),
]

_VIOLATION_PARAMS = [
    pytest.param(s, case_name, broken, id=f"{s.name}-{case_name}")
    for s in SURFACES
    for case_name, broken in schema_violations(s.schema, s.valid())
]


# ---------------------------------------------------------------------------
# Tool-use surfaces x the battery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("surface", "case_name", "broken"), _VIOLATION_PARAMS)
async def test_schema_violation_is_rejected_at_the_boundary(
    surface: Surface, case_name: str, broken: dict[str, Any]
) -> None:
    """A schema-violating tool input must raise ValidationError at the
    complete_json boundary — never leak a half-parsed object downstream."""
    llm = MockLLMClient(scripted={surface.purpose: json.dumps(broken)})
    with pytest.raises(ValidationError):
        await surface.call(llm)


@pytest.mark.parametrize("surface", SURFACES, ids=lambda s: s.name)
async def test_malformed_tool_json_raises(surface: Surface) -> None:
    """Non-JSON scripted output models the provider failing to emit tool_use;
    the mock raises JSONDecodeError, mirroring the real client's failure."""
    llm = MockLLMClient(scripted={surface.purpose: TEXT_EDGES["malformed_json"]})
    with pytest.raises(json.JSONDecodeError):
        await surface.call(llm)


@pytest.mark.parametrize("surface", SURFACES, ids=lambda s: s.name)
async def test_provider_error_propagates(surface: Surface) -> None:
    """A responder that raises models a provider 5xx mid-call — the surface
    must propagate (callers convert to HTTP errors), not swallow it."""

    def _explode(latest_user: str, _messages: list[Message]) -> str:
        raise RuntimeError("provider 500")

    llm = MockLLMClient(scripted={surface.purpose: _explode})
    with pytest.raises(RuntimeError, match="provider 500"):
        await surface.call(llm)


@pytest.mark.parametrize("surface", SURFACES[:2], ids=lambda s: s.name)
async def test_injection_content_is_treated_as_data(surface: Surface) -> None:
    """Adversarial content in schema-valid string fields must round-trip
    VERBATIM — parsed as data, never interpreted, stripped, or acted on.
    (tailor is excluded: its trace-ref post-validation rejects rewritten
    bullets by design, which is its own guard, covered below.)"""
    payload = inject_into_strings(surface.valid(), INJECTION_TEXT)
    llm = MockLLMClient(scripted={surface.purpose: json.dumps(payload)})
    result = await surface.call(llm)
    assert surface.text_field(result) == INJECTION_TEXT


async def test_tailor_injection_survives_in_summary() -> None:
    """Same data-not-instructions probe for tailor, mutating only ``summary``
    so the traceability post-validation still passes."""
    payload = _valid_resume_dict()
    payload["summary"] = INJECTION_TEXT
    llm = MockLLMClient(scripted={"tailor.resume": json.dumps(payload)})
    resume, _warnings, _result = await tailor_resume(
        llm,
        optimized=_optimized(),
        job_description="We want a senior FE.",
        contact=ContactInfo(name="Test User", email="test@example.com"),
    )
    assert resume.summary == INJECTION_TEXT


async def test_unicode_content_round_trips_exactly() -> None:
    payload = _valid_optimized_dict()
    payload["summary"] = UNICODE_TEXT
    llm = MockLLMClient(scripted={"experience.derive": json.dumps(payload)})
    parsed, _result = await derive_from_prose(llm, prose_text="prose")
    assert parsed.summary == UNICODE_TEXT


def test_empty_object_validates_as_empty_optimized_payload() -> None:
    """PINNED FINDING: OptimizedPayload has zero required fields, so a model
    returning ``{}`` validates as an empty payload (no roles/skills/outcomes).
    Downstream the gap-gate blocks generation from it, but a silent
    empty-derive is a product-level edge worth this explicit pin — if this
    test ever fails because required fields were added, update the corpus
    expectations deliberately."""
    payload = OptimizedPayload.model_validate({})
    assert payload.roles == [] and payload.skills == [] and payload.outcomes == []


# ---------------------------------------------------------------------------
# The raw-text parse surface (derive/stream's inline parse)
# ---------------------------------------------------------------------------


def _stream_parse(text: str) -> OptimizedPayload:
    """Exactly what routers/experience.py's derive/stream does with the
    buffered completion text."""
    return OptimizedPayload.model_validate_json(strip_markdown_fence(text))


def test_stream_parse_strips_markdown_fence() -> None:
    valid_json = json.dumps(_valid_optimized_dict())
    parsed = _stream_parse(fenced(valid_json))
    assert parsed.roles[0].company == "FightCamp"


@pytest.mark.parametrize("case_name", sorted(TEXT_EDGES))
def test_stream_parse_rejects_text_edges(case_name: str) -> None:
    with pytest.raises(ValidationError):
        _stream_parse(TEXT_EDGES[case_name])


def test_stream_parse_rejects_truncated_json() -> None:
    with pytest.raises(ValidationError):
        _stream_parse(truncate_json(json.dumps(_valid_optimized_dict())))
