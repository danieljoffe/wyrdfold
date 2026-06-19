"""Résumé-free vs résumé-grounded label derivation (#78 layer 1).

Pins the flag switch in ``derive_profile_from_label``:

* ``resume_free_label_derivation`` on (or ``payload is None``) → the
  role-generic system prompt + the label ALONE, so a shared target's
  rubric isn't skewed by whoever activated it;
* default (flag off, payload present) → the grounded prompt with the
  user's experience appended (historical behavior).

Either way the résumé still feeds ``fit_score`` separately; this only
governs the shared scoring rubric.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.services.targets import derive_profile_from_label as mod
from app.services.targets.derive_profile_from_label import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_GENERIC,
    derive_profile_from_label,
)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the (system, messages) passed to complete_json so we can
    assert which prompt + content the function chose, without an LLM."""
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
async def test_resume_free_uses_generic_prompt_label_only(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    monkeypatch.setattr(settings, "resume_free_label_derivation", True)
    # The résumé serializer must NOT be touched in résumé-free mode.
    monkeypatch.setattr(
        mod, "_build_user_message", lambda _p: pytest.fail("résumé used in free mode")
    )

    derived, result = await derive_profile_from_label(
        object(), label="Senior Frontend Engineer", payload=object()
    )

    assert (derived, result) == ("DERIVED", "RESULT")
    assert captured["system"] is SYSTEM_PROMPT_GENERIC
    assert captured["messages"][0].content == "Target role: Senior Frontend Engineer"
    assert captured["cache_system"] is True


@pytest.mark.asyncio
async def test_default_grounds_profile_in_resume(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    monkeypatch.setattr(settings, "resume_free_label_derivation", False)
    monkeypatch.setattr(mod, "_build_user_message", lambda _p: "USER_CONTEXT_BLOCK")

    await derive_profile_from_label(object(), label="Staff Engineer", payload=object())

    assert captured["system"] is SYSTEM_PROMPT
    content = captured["messages"][0].content
    assert content.startswith("Target role: Staff Engineer")
    assert "USER_CONTEXT_BLOCK" in content


@pytest.mark.asyncio
async def test_none_payload_falls_back_to_generic(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    """Even with the flag off, a missing payload can't ground anything —
    degrade to the generic prompt rather than crash on _build_user_message."""
    monkeypatch.setattr(settings, "resume_free_label_derivation", False)
    monkeypatch.setattr(mod, "_build_user_message", lambda _p: pytest.fail("résumé used with None"))

    await derive_profile_from_label(object(), label="Product Manager", payload=None)

    assert captured["system"] is SYSTEM_PROMPT_GENERIC
    assert captured["messages"][0].content == "Target role: Product Manager"


def test_generic_prompt_is_role_generic_not_resume_grounded() -> None:
    """The point of #78 layer 1: the generic prompt must not instruct
    grounding in the user's actual experience, and the grounded one must."""
    assert "role-generic" in SYSTEM_PROMPT_GENERIC
    assert "ACTUAL experience" not in SYSTEM_PROMPT_GENERIC
    assert "ACTUAL experience" in SYSTEM_PROMPT
