"""Signature-validating stand-ins for the Anthropic SDK (#1065).

Three tests once asserted that a ``MagicMock`` standing in for
``messages.create`` had RECEIVED ``temperature=0.0`` — a kwarg anthropic 1.0.0
had already removed. A permissive mock cannot tell "the client sends what the
SDK accepts" from "the client sends what the test expects", so the bug lived
on for weeks with a green suite.

These fakes bind every call against the INSTALLED SDK's real signature. A
stale or removed kwarg is a ``TypeError`` here — the same one the SDK raises —
with no network, the day the dependency changes.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from anthropic import AsyncAnthropic


def real_messages_create_signature() -> inspect.Signature:
    """The bound signature of ``AsyncAnthropic(...).messages.create`` as
    installed — the one authority on which kwargs exist."""
    return inspect.signature(AsyncAnthropic(api_key="signature-probe").messages.create)


def validating_create_mock(response: Any) -> AsyncMock:
    """An ``AsyncMock`` for ``messages.create`` that raises ``TypeError`` for
    any kwarg the installed SDK would reject (and for missing required ones),
    then returns ``response``. ``call_args`` is recorded as usual."""
    sig = real_messages_create_signature()

    async def _validate_then_return(*args: Any, **kwargs: Any) -> Any:
        sig.bind(*args, **kwargs)  # raises exactly as the real method would
        return response

    return AsyncMock(side_effect=_validate_then_return)


def tool_use_response(
    tool_input: dict[str, Any],
    *,
    tool_name: str = "return_X",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> Any:
    """A minimal response carrying one ``tool_use`` block, shaped like the SDK's."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input

    response = MagicMock()
    response.content = [block]
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    response.usage.cache_read_input_tokens = 0
    response.usage.cache_creation_input_tokens = 0
    response.usage.model_extra = MagicMock()
    response.stop_reason = "tool_use"
    return response
