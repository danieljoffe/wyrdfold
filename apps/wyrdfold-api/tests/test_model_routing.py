"""The routing matrix, walked for EVERY ModelId (#1065).

The bug this encodes: anthropic 1.0.0 removed ``temperature`` from
``messages.create``. Every hard-coded Claude call site then raised
``TypeError`` on its first structured call — invisibly, because the SDK was
mocked permissively in tests and production traffic only exercised DeepSeek's
OpenAI-shaped path. One site swallowed the error and silently degraded the
catalog dedup key.

These tests walk every ``ModelId`` literal through BOTH real client classes
with a signature-validating SDK fake (``tests/support/sdk_fakes.py``). The day
a kwarg goes stale, this fails with no network — which is what would have
caught #905 the day it merged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest

from app.models.llm import Message, ModelId
from app.services.llm.anthropic_client import (
    _TEMPERATURE_ACCEPTING_MODELS,
    AnthropicLLMClient,
)
from app.services.llm.openrouter_client import (
    _MODEL_SLUG_MAP,
    _OPENAI_SHAPED_MODELS,
    _ROUTES,
    OpenRouterLLMClient,
)
from tests.support.sdk_fakes import tool_use_response, validating_create_mock

ALL_MODELS: tuple[str, ...] = get_args(ModelId)
_REMOVED_SAMPLING_KWARGS = {"temperature", "top_p", "top_k"}
_APP_DIR = Path(__file__).resolve().parents[1] / "app"


async def _forced_tool_call(client: Any, model: str) -> None:
    await client.complete_tool_use(
        model=model,
        system="s",
        messages=[Message(role="user", content="x")],
        tool_name="return_X",
        tool_description="d",
        tool_input_schema={"type": "object", "properties": {}},
        purpose="test.routing",
        temperature=0.0,
    )


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


def test_every_model_id_has_exactly_one_route() -> None:
    """A ModelId with no row cannot be routed; a row with no ModelId is dead.
    Both are drift between two places that must move together."""
    assert set(_ROUTES) == set(ALL_MODELS), {
        "missing_route": sorted(set(ALL_MODELS) - set(_ROUTES)),
        "orphan_route": sorted(set(_ROUTES) - set(ALL_MODELS)),
    }


def test_derived_views_are_projections_of_the_table() -> None:
    """``_MODEL_SLUG_MAP`` and ``_OPENAI_SHAPED_MODELS`` are kept as names
    for readability; they must never be edited independently of ``_ROUTES``."""
    assert {m: r.slug for m, r in _ROUTES.items()} == _MODEL_SLUG_MAP
    assert frozenset(m for m, r in _ROUTES.items() if r.shape == "openai") == _OPENAI_SHAPED_MODELS


def test_temperature_allowlist_is_a_subset_of_model_id() -> None:
    """An allowlist entry that is not a ModelId can never match — silently
    dropping the hint for the model someone meant to keep it on."""
    assert set(ALL_MODELS) >= _TEMPERATURE_ACCEPTING_MODELS


# ---------------------------------------------------------------------------
# Direct Anthropic provider (llm_provider=anthropic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ALL_MODELS)
async def test_direct_client_never_sends_a_removed_sampling_kwarg(model: str) -> None:
    client = AnthropicLLMClient(api_key="sk-ant-fake")
    create = validating_create_mock(tool_use_response({"ok": True}))
    client._client.messages.create = create  # type: ignore[method-assign]

    await _forced_tool_call(client, model)  # TypeError here = a stale kwarg

    kw = create.call_args.kwargs
    assert not (_REMOVED_SAMPLING_KWARGS & kw.keys()), kw.keys()
    if model in _TEMPERATURE_ACCEPTING_MODELS:
        assert kw["extra_body"] == {"temperature": 0.0}
    else:
        assert "extra_body" not in kw


# ---------------------------------------------------------------------------
# OpenRouter provider — the inheritance path is where the bug actually lived
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", [m for m in ALL_MODELS if m not in _OPENAI_SHAPED_MODELS])
async def test_openrouter_anthropic_shaped_path_keys_allowlist_on_internal_id(model: str) -> None:
    """Review correction on #1065: OpenRouter resolves ``claude-sonnet-4-6``
    to ``anthropic/claude-sonnet-4.6``. Keying the allowlist on the RESOLVED
    slug would omit the hint on exactly the path this fix repairs. So the
    request must carry the resolved slug as ``model`` AND, for accepting
    models, the hint in ``extra_body``."""
    client = OpenRouterLLMClient(api_key="sk-or-fake")
    create = validating_create_mock(tool_use_response({"ok": True}))
    client._client.messages.create = create  # type: ignore[method-assign]

    await _forced_tool_call(client, model)

    kw = create.call_args.kwargs
    assert kw["model"] == _ROUTES[model].slug
    assert not (_REMOVED_SAMPLING_KWARGS & kw.keys()), kw.keys()
    if model in _TEMPERATURE_ACCEPTING_MODELS:
        assert kw["extra_body"] == {"temperature": 0.0}
    else:
        assert "extra_body" not in kw


@pytest.mark.parametrize("model", sorted(_OPENAI_SHAPED_MODELS))
async def test_openrouter_openai_shaped_path_receives_temperature_directly(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DeepSeek honours ``temperature`` in the /chat/completions body, so the
    hint is forwarded there as-is. (The body itself is asserted in
    ``test_openrouter_openai.py``; this proves DISPATCH for every
    OpenAI-shaped id.)"""
    client = OpenRouterLLMClient(api_key="sk-or-fake")
    seen: dict[str, Any] = {}

    async def _recorder(**kwargs: Any) -> tuple[dict[str, Any], Any]:
        seen.update(kwargs)
        return {"ok": True}, MagicMock()

    monkeypatch.setattr(client, "_openai_tool_use", _recorder)
    await _forced_tool_call(client, model)

    assert seen["model"] == model
    assert seen["temperature"] == 0.0


# ---------------------------------------------------------------------------
# Hard-coded model constants — the call sites that bypass settings
# ---------------------------------------------------------------------------

# ``DEFAULT_MODEL: ModelId = "claude-sonnet-4-6"`` (module constants) and
# ``phase2_fit_model: ModelId = "claude-sonnet-4-6"`` (Settings defaults).
_MODEL_CONSTANT = re.compile(r'^\s*[A-Za-z_][A-Za-z_0-9]*\s*:\s*ModelId\s*=\s*"([^"]+)"', re.M)


def test_every_hard_coded_model_constant_is_a_known_model_id() -> None:
    """Seventeen modules pin a Claude model as a constant with no settings
    override — target creation, experience, conversation, tailor, analysis,
    the learner. Those are the call sites that reach the SDK path in
    production the moment a user acts. Each must be a ModelId, so the
    parametrized tests above cover it; a typo here would surface only at
    runtime, on exactly that path."""
    found: dict[str, list[str]] = {}
    for path in _APP_DIR.rglob("*.py"):
        for match in _MODEL_CONSTANT.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1), []).append(str(path.relative_to(_APP_DIR)))

    assert found, "scan found no hard-coded ModelId constants — the regex is broken"
    unknown = {m: files for m, files in found.items() if m not in ALL_MODELS}
    assert not unknown, f"constants pin ids that are not ModelIds: {unknown}"
