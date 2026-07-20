"""MockLLMClient — deterministic fake for tests and local dev.

Two modes:
1. Scripted responses: register `(purpose, response_text)` pairs; calls with
   a matching purpose return that text. Good for unit tests where the exact
   response matters.
2. Echo mode (default): the client synthesizes a predictable response from
   the latest user message. Useful for integration tests where we care about
   the pipeline, not the content.

Both modes compute realistic-ish token counts (roughly 4 chars/token) and
apply real pricing so cost-log rows look sensible when inspected.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.models.llm import (
    LLMResult,
    LLMStreamDelta,
    LLMStreamEvent,
    LLMStreamFinal,
    LLMUsage,
    Message,
    ModelId,
)
from app.services.llm.pricing import calculate_cost


def _approx_tokens(text: str) -> int:
    """Rough char-to-token heuristic. Good enough for a mock."""
    return max(1, len(text) // 4)


ResponseSource = str | Callable[[str, list[Message]], str]


# Kept in sync with ``targets.suggest.QUERY_DEFAULT_PURPOSE``. Duplicated (not
# imported) to keep the mock free of service-layer imports.
QUERY_SUGGEST_PURPOSE = "target.suggest_from_query"


# Seniority words we strip off the front of a query so we can rebuild a small
# ladder of adjacent-seniority neighbours around the role's "core".
_SENIORITY_PREFIXES = frozenset(
    {"junior", "jr", "mid", "mid-level", "senior", "sr", "staff", "principal", "lead", "head"}
)


def _dev_suggest_from_query(latest_user: str, _messages: list[Message]) -> str:
    """Deterministic happy-path suggestions for the ``target.suggest_from_query``
    purpose, used for local dev / integration-through-DI (never in tests, which
    register their own scripted responses).

    The query-suggest service puts the raw query on the first line of the user
    message (see ``suggest._build_query_message``). We echo it back as the
    canonical first suggestion, then add a couple of adjacent-seniority
    neighbours so the local search UI shows a realistic selectable list. This
    is a fake — the real LLM tailors these to the query and the user's
    experience; the descriptions say so plainly.
    """
    first_line = next((line.strip() for line in latest_user.splitlines() if line.strip()), "")
    query = first_line[:120] or "Target Role"
    canonical = query.title()
    words = canonical.split()
    core = (
        " ".join(words[1:])
        if words and words[0].lower() in _SENIORITY_PREFIXES and len(words) > 1
        else canonical
    )

    seen: set[str] = set()
    labels: list[str] = []
    for label in (canonical, f"Senior {core}", f"Staff {core}", f"Principal {core}"):
        key = label.lower()
        if key not in seen:
            seen.add(key)
            labels.append(label)
        if len(labels) >= 4:
            break

    suggestions = [
        {
            "label": label,
            "description": (
                f"Roles similar to “{query}”. (Local mock suggestion — the real LLM tailors these.)"
            ),
            "core_skills": [],
        }
        for label in labels
    ]
    return json.dumps({"suggestions": suggestions})


def dev_default_responses() -> dict[str, ResponseSource]:
    """Scripted responses seeded into the mock for LOCAL DEV / integration only
    (the ``LLM_PROVIDER=mock`` factory), so LLM-backed flows return usable data
    instead of the bare ``{"mock": True}`` echo.

    Unit tests construct ``MockLLMClient()`` directly (no seed) and register
    their own responses, so this never changes test behavior. A fresh dict is
    returned each call so callers can mutate their own copy.
    """
    return {QUERY_SUGGEST_PURPOSE: _dev_suggest_from_query}


class MockLLMClient:
    """Implements the LLMClient Protocol. Not used in production."""

    def __init__(
        self,
        *,
        scripted: dict[str, ResponseSource] | None = None,
        default_latency_ms: int = 50,
    ) -> None:
        self._scripted: dict[str, ResponseSource] = scripted or {}
        self._default_latency_ms = default_latency_ms
        self.calls: list[dict[str, object]] = []

    def register(self, purpose: str, response: ResponseSource) -> None:
        """Register a scripted response for a given purpose label."""
        self._scripted[purpose] = response

    async def complete(
        self,
        *,
        model: ModelId,
        system: str,
        messages: list[Message],
        purpose: str,
        max_tokens: int = 4096,
        cache_system: bool = False,
    ) -> LLMResult:
        if not messages:
            raise ValueError("MockLLMClient.complete requires at least one message")

        latest_user = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            messages[-1].content,
        )

        response_text = self._render_response(purpose, latest_user, messages)

        usage = LLMUsage(
            input_tokens=_approx_tokens(system) + sum(_approx_tokens(m.content) for m in messages),
            output_tokens=_approx_tokens(response_text),
            cache_read_input_tokens=0,
            cache_creation_input_tokens=_approx_tokens(system) if cache_system else 0,
        )

        cost = calculate_cost(model, usage)

        self.calls.append(
            {
                "model": model,
                "purpose": purpose,
                "system_len": len(system),
                "messages_count": len(messages),
                "messages": list(messages),
                "cache_system": cache_system,
                "max_tokens": max_tokens,
            }
        )

        return LLMResult(
            content=response_text,
            model=model,
            usage=usage,
            cost_usd=cost,
            latency_ms=self._default_latency_ms,
        )

    async def complete_tool_use(
        self,
        *,
        model: ModelId,
        system: str,
        messages: list[Message],
        tool_name: str,
        tool_description: str,
        tool_input_schema: dict[str, Any],
        purpose: str,
        max_tokens: int = 4096,
        cache_system: bool = False,
        temperature: float | None = None,
    ) -> tuple[dict[str, Any], LLMResult]:
        """Mock structured-output. Scripted responses are parsed as JSON
        and returned as the tool input dict; echo mode returns a small
        echo dict. Tests that script invalid JSON exercise the error path
        the real client would also raise on (server-side schema rejection
        or tool_use absence).
        """
        if not messages:
            raise ValueError("MockLLMClient.complete_tool_use requires at least one message")

        latest_user = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            messages[-1].content,
        )
        response_text = self._render_response(purpose, latest_user, messages)
        # Will raise json.JSONDecodeError if scripted text is not valid JSON
        # — mirrors the real client failing when the model emits no tool_use.
        tool_input = json.loads(response_text)
        if not isinstance(tool_input, dict):
            raise ValueError(
                f"Scripted response for {purpose!r} must decode to a JSON object, "
                f"got {type(tool_input).__name__}"
            )

        usage = LLMUsage(
            input_tokens=_approx_tokens(system) + sum(_approx_tokens(m.content) for m in messages),
            output_tokens=_approx_tokens(response_text),
            cache_read_input_tokens=0,
            cache_creation_input_tokens=_approx_tokens(system) if cache_system else 0,
        )
        cost = calculate_cost(model, usage)

        self.calls.append(
            {
                "model": model,
                "purpose": purpose,
                "system_len": len(system),
                "messages_count": len(messages),
                "messages": list(messages),
                "cache_system": cache_system,
                "max_tokens": max_tokens,
                "tool_name": tool_name,
            }
        )

        return tool_input, LLMResult(
            content=response_text,
            model=model,
            usage=usage,
            cost_usd=cost,
            latency_ms=self._default_latency_ms,
        )

    async def stream(
        self,
        *,
        model: ModelId,
        system: str,
        messages: list[Message],
        purpose: str,
        max_tokens: int = 4096,
        cache_system: bool = False,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Mock streaming: yields the scripted response in fixed-size chunks
        and finishes with a single final event. Mirrors the cost/usage shape
        of `complete` so consumers can use either interchangeably.
        """
        if not messages:
            raise ValueError("MockLLMClient.stream requires at least one message")

        latest_user = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            messages[-1].content,
        )

        response_text = self._render_response(purpose, latest_user, messages)

        chunk_size = 32
        for i in range(0, len(response_text), chunk_size):
            yield LLMStreamDelta(text=response_text[i : i + chunk_size])

        usage = LLMUsage(
            input_tokens=_approx_tokens(system) + sum(_approx_tokens(m.content) for m in messages),
            output_tokens=_approx_tokens(response_text),
            cache_read_input_tokens=0,
            cache_creation_input_tokens=_approx_tokens(system) if cache_system else 0,
        )
        cost = calculate_cost(model, usage)

        self.calls.append(
            {
                "model": model,
                "purpose": purpose,
                "system_len": len(system),
                "messages_count": len(messages),
                "messages": list(messages),
                "cache_system": cache_system,
                "max_tokens": max_tokens,
                "streamed": True,
            }
        )

        yield LLMStreamFinal(
            result=LLMResult(
                content=response_text,
                model=model,
                usage=usage,
                cost_usd=cost,
                latency_ms=self._default_latency_ms,
            )
        )

    def _render_response(self, purpose: str, latest_user: str, messages: list[Message]) -> str:
        source = self._scripted.get(purpose)
        if source is None:
            return json.dumps({"mock": True, "purpose": purpose, "echo": latest_user})
        if callable(source):
            return source(latest_user, messages)
        return source
