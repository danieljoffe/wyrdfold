"""Tests for the lateral target discovery service (PR D)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.models.experience import OptimizedPayload
from app.models.targets import (
    JobTarget,
    ScoringProfile,
)
from app.services.targets.lateral_discovery import (
    LateralSuggestion,
    LateralSuggestions,
    _build_user_message,
    suggest_lateral_targets,
)


def _payload() -> OptimizedPayload:
    return OptimizedPayload(summary="...", roles=[], skills=[], outcomes=[])


def _target(label: str, *, seniority: str | None = None) -> JobTarget:
    return JobTarget(
        id=f"t-{label.lower().replace(' ', '-')}",
        label=label,
        scoring_profile=ScoringProfile(),
        is_active=True,
        seniority_hint=seniority,  # type: ignore[arg-type]
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ---- _build_user_message --------------------------------------------------


def test_user_message_includes_profile_summary() -> None:
    msg = _build_user_message(_payload(), [])
    assert "## User profile" in msg


def test_user_message_lists_current_targets_for_exclusion() -> None:
    msg = _build_user_message(
        _payload(),
        [
            _target("Director of CX Operations"),
            _target("Head of Customer Experience"),
        ],
    )
    assert "do NOT re-suggest" in msg
    assert "Director of CX Operations" in msg
    assert "Head of Customer Experience" in msg


def test_user_message_includes_seniority_hint_when_present() -> None:
    msg = _build_user_message(
        _payload(),
        [_target("Director of CX Operations", seniority="director")],
    )
    assert "(director)" in msg


def test_user_message_handles_no_current_targets_gracefully() -> None:
    msg = _build_user_message(_payload(), [])
    # Notes the empty state explicitly rather than rendering a bare
    # header. The LLM picks up the cue that this is a first-time call.
    assert "none" in msg.lower() or "first lateral pass" in msg.lower()


def test_user_message_states_task_with_cap() -> None:
    msg = _build_user_message(_payload(), [])
    assert "lateral targets" in msg
    # Should mention the cap so the LLM doesn't run away.
    assert "8" in msg  # _MAX_SUGGESTIONS


# ---- LateralSuggestion schema --------------------------------------------


def test_lateral_suggestion_requires_seniority_hint() -> None:
    """seniority_hint is REQUIRED — the activation flow needs it to map
    onto the slim target shape's seniority_hint column."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LateralSuggestion(
            label="Director of CX Ops",
            one_line_reasoning="x",
            confidence=80,
            lateral_relationship="same altitude",
        )  # type: ignore[call-arg]


def test_lateral_suggestion_seniority_enum_enforced() -> None:
    """Only the canonical 7 seniority levels are accepted."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LateralSuggestion(
            label="Director of CX Ops",
            one_line_reasoning="x",
            confidence=80,
            lateral_relationship="same altitude",
            seniority_hint="overlord",  # type: ignore[arg-type]
        )


def test_lateral_suggestion_confidence_bounds() -> None:
    from pydantic import ValidationError

    # In-bounds
    LateralSuggestion(
        label="VP of CX",
        one_line_reasoning="x",
        confidence=0,
        lateral_relationship="x",
        seniority_hint="director",
    )
    LateralSuggestion(
        label="VP of CX",
        one_line_reasoning="x",
        confidence=100,
        lateral_relationship="x",
        seniority_hint="director",
    )
    # Out of bounds rejected
    with pytest.raises(ValidationError):
        LateralSuggestion(
            label="VP of CX",
            one_line_reasoning="x",
            confidence=101,
            lateral_relationship="x",
            seniority_hint="director",
        )
    with pytest.raises(ValidationError):
        LateralSuggestion(
            label="VP of CX",
            one_line_reasoning="x",
            confidence=-1,
            lateral_relationship="x",
            seniority_hint="director",
        )


# ---- suggest_lateral_targets (mocked LLM) --------------------------------


@pytest.mark.asyncio
async def test_returns_suggestions_from_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: LLM returns 5 suggestions, all within the cap."""
    fixture = LateralSuggestions(
        suggestions=[
            LateralSuggestion(
                label="Director of Customer Success Operations",
                one_line_reasoning="Maps onto your Zendesk + BPO experience.",
                confidence=92,
                lateral_relationship="same altitude, CS vocab",
                primary_industry="B2B SaaS",
                seniority_hint="director",
            ),
            LateralSuggestion(
                label="Head of Member Experience",
                one_line_reasoning="Healthtech framing of your CX work.",
                confidence=78,
                lateral_relationship="same altitude, healthtech industry",
                primary_industry="healthtech",
                seniority_hint="director",
            ),
        ]
    )

    async def fake_complete_json(*args: object, **kwargs: object) -> object:
        return (fixture, MagicMock())

    monkeypatch.setattr(
        "app.services.targets.lateral_discovery.complete_json",
        fake_complete_json,
    )

    parsed, _ = await suggest_lateral_targets(MagicMock(), payload=_payload())
    assert len(parsed.suggestions) == 2
    assert parsed.suggestions[0].label == "Director of Customer Success Operations"


@pytest.mark.asyncio
async def test_trims_oversized_response_by_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LLM ignores the 8-max instruction and returns 12, we keep
    the top 8 by confidence rather than rejecting the whole batch."""
    over = LateralSuggestions(
        suggestions=[
            LateralSuggestion(
                label=f"Target {i}",
                one_line_reasoning="x",
                confidence=i * 10 % 95,  # spread across 0-95
                lateral_relationship="x",
                seniority_hint="director",
            )
            for i in range(12)
        ]
    )

    async def fake_complete_json(*args: object, **kwargs: object) -> object:
        return (over, MagicMock())

    monkeypatch.setattr(
        "app.services.targets.lateral_discovery.complete_json",
        fake_complete_json,
    )

    parsed, _ = await suggest_lateral_targets(MagicMock(), payload=_payload())
    assert len(parsed.suggestions) == 8
    # Sorted highest-confidence first — the top 8 of the original 12 by score.
    confidences = [s.confidence for s in parsed.suggestions]
    assert confidences == sorted(confidences, reverse=True)


@pytest.mark.asyncio
async def test_passes_current_targets_to_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current_targets list must reach the prompt or the LLM will
    happily re-suggest what the user already has."""
    seen_messages: list[str] = []

    async def fake_complete_json(*args: object, **kwargs: object) -> object:
        # The user message is in ``messages[0].content``.
        seen_messages.append(kwargs["messages"][0].content)
        return (LateralSuggestions(suggestions=[]), MagicMock())

    monkeypatch.setattr(
        "app.services.targets.lateral_discovery.complete_json",
        fake_complete_json,
    )

    await suggest_lateral_targets(
        MagicMock(),
        payload=_payload(),
        current_targets=[_target("Director of CX Operations")],
    )

    assert len(seen_messages) == 1
    assert "Director of CX Operations" in seen_messages[0]
    assert "do NOT re-suggest" in seen_messages[0]


# ---- verbose-prose tolerance (2026-07-14 live 500) ---------------------------
#
# Two of eight suggestions ran a sentence past lateral_relationship's
# 180-char display cap and the WHOLE response 500'd. The caps are a UI
# contract, not a correctness bound — over-long prose is truncated at a
# word boundary now (same doctrine as the tagger's tolerate-malformed
# fix). Structural garbage must still fail loud.


def _valid_kwargs(**overrides: object) -> dict:
    base: dict = {
        "label": "Director of CX Ops",
        "one_line_reasoning": "Ran a 40-agent org through a platform swap.",
        "confidence": 80,
        "lateral_relationship": "Same altitude, different industry vocabulary.",
        "seniority_hint": "director",
    }
    base.update(overrides)
    return base


def test_overlong_lateral_relationship_is_truncated_not_fatal() -> None:
    # Mirror the incident: a verbose ~230-char relationship string.
    verbose = (
        "Same altitude, platform-operations flavored — your run of large "
        "migrations maps directly, though the vocabulary here leans heavily "
        "on marketplace tooling which differs from the recruiter's prior "
        "industry mix and needs a reframe."
    )
    assert len(verbose) > 180
    s = LateralSuggestion.model_validate(_valid_kwargs(lateral_relationship=verbose))
    assert len(s.lateral_relationship) <= 180
    assert s.lateral_relationship.endswith("…")
    # Word-boundary cut: no mid-word tail before the ellipsis.
    assert not s.lateral_relationship[:-1].endswith(" ")


def test_all_prose_caps_are_tolerant() -> None:
    s = LateralSuggestion.model_validate(
        _valid_kwargs(
            label="L" + "x" * 200,
            one_line_reasoning="r" * 300,
            lateral_relationship="q" * 300,
            primary_industry="i" * 120,
        )
    )
    assert len(s.label) <= 120
    assert len(s.one_line_reasoning) <= 240
    assert len(s.lateral_relationship) <= 180
    assert s.primary_industry is not None and len(s.primary_industry) <= 80


def test_exact_cap_passes_untouched() -> None:
    exact = "x" * 180
    s = LateralSuggestion.model_validate(_valid_kwargs(lateral_relationship=exact))
    assert s.lateral_relationship == exact


def test_structural_garbage_still_fails_loud() -> None:
    from pydantic import ValidationError

    # Wrong TYPE is a real malformed response, not verbosity — must raise.
    with pytest.raises(ValidationError):
        LateralSuggestion.model_validate(
            _valid_kwargs(lateral_relationship=["not", "a", "string"])
        )
    # And a too-short label stays a hard error (min_length is a floor,
    # not a prose cap).
    with pytest.raises(ValidationError):
        LateralSuggestion.model_validate(_valid_kwargs(label="X"))


@pytest.mark.asyncio
async def test_full_path_survives_verbose_model_via_mock() -> None:
    """The 2026-07-14 incident as a mock behavior: the model's tool-call
    payload overflows two prose caps; the real complete_json round trip
    (schema validation included) must return truncated suggestions, not
    raise. Grows the mock's edge battery per the LLM-surface rule."""
    import json

    from app.services.llm.mock import MockLLMClient

    verbose_payload = {
        "suggestions": [
            {
                "label": "Marketplace Operations Director",
                "one_line_reasoning": "Your platform-migration record maps over.",
                "confidence": 74,
                "lateral_relationship": (
                    "Same altitude, platform-operations flavored — your run of "
                    "large migrations maps directly, though the vocabulary here "
                    "leans heavily on marketplace tooling which differs from the "
                    "recruiter's prior industry mix."
                ),
                "primary_industry": "marketplaces",
                "seniority_hint": "director",
            }
        ]
    }
    mock = MockLLMClient()
    mock.register("target.suggest_lateral", json.dumps(verbose_payload))

    parsed, result = await suggest_lateral_targets(mock, payload=_payload())

    assert result is not None
    assert len(parsed.suggestions) == 1
    assert len(parsed.suggestions[0].lateral_relationship) <= 180
    assert parsed.suggestions[0].lateral_relationship.endswith("…")
