"""Label-only target derivation (#5 layer 1 / #78).

The shared scoring rubric is built from the LABEL ALONE — role-generic world
knowledge — never an individual's résumé, which feeds ``fit_score`` separately.
This pins that the derivation uses the generic prompt + label-only message and
never reaches for résumé content.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.targets import derive_profile_from_label as mod
from app.services.targets.derive_profile_from_label import (
    SYSTEM_PROMPT_GENERIC,
    derive_profile_from_label,
)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the (system, messages) passed to complete_json so we can assert
    which prompt + content the function chose, without an LLM."""
    seen: dict[str, Any] = {}

    async def fake_complete_json(
        llm: Any,
        *,
        model: Any,
        system: str,
        messages: list[Any],
        schema: Any,
        purpose: str,
        cache_system: bool,
    ) -> tuple[str, str]:
        seen["system"] = system
        seen["messages"] = messages
        seen["cache_system"] = cache_system
        return ("DERIVED", "RESULT")

    monkeypatch.setattr(mod, "complete_json", fake_complete_json)
    return seen


@pytest.mark.asyncio
async def test_derivation_is_label_only_via_generic_prompt(
    captured: dict[str, Any],
) -> None:
    derived, result = await derive_profile_from_label(
        object(), label="Senior Frontend Engineer"
    )

    assert (derived, result) == ("DERIVED", "RESULT")
    assert captured["system"] is SYSTEM_PROMPT_GENERIC
    assert captured["messages"][0].content == "Target role: Senior Frontend Engineer"
    assert captured["cache_system"] is True


def test_generic_prompt_is_role_generic_not_resume_grounded() -> None:
    """The point of layer 1: the prompt must instruct role-generic derivation,
    not grounding in any individual's actual experience."""
    assert "role-generic" in SYSTEM_PROMPT_GENERIC
    assert "ACTUAL experience" not in SYSTEM_PROMPT_GENERIC
