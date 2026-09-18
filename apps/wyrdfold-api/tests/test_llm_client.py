"""LLMClient helper behavior — schema memoization and tool-name sanitization."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from app.models.llm import Message
from app.services.llm.client import (
    _tool_input_schema_for,
    _tool_name_for,
    complete_json,
)
from app.services.llm.errors import LLMMalformedOutputError
from app.services.llm.mock import MockLLMClient


class _Schema(BaseModel):
    name: str
    count: int = 0


def test_tool_name_for_caches_per_schema() -> None:
    _tool_name_for.cache_clear()
    a = _tool_name_for(_Schema)
    b = _tool_name_for(_Schema)
    assert a == b
    info = _tool_name_for.cache_info()
    assert info.hits >= 1


def test_tool_input_schema_for_caches_per_schema() -> None:
    _tool_input_schema_for.cache_clear()
    a = _tool_input_schema_for(_Schema)
    b = _tool_input_schema_for(_Schema)
    # Same object identity proves the cache was used (avoiding a fresh
    # `model_json_schema()` call on every invocation).
    assert a is b
    info = _tool_input_schema_for.cache_info()
    assert info.hits >= 1


def test_tool_input_schema_for_distinguishes_classes() -> None:
    class _Other(BaseModel):
        flag: bool

    _tool_input_schema_for.cache_clear()
    a = _tool_input_schema_for(_Schema)
    b = _tool_input_schema_for(_Other)
    assert a is not b
    assert "name" in a["properties"]
    assert "flag" in b["properties"]


async def test_complete_json_round_trips_schema_via_mock() -> None:
    payload = {"name": "alpha", "count": 7}

    client = MockLLMClient(scripted={"test.schema": json.dumps(payload)})
    parsed, result = await complete_json(
        client,
        model="claude-haiku-4-5",
        system="sys",
        messages=[Message(role="user", content="ignored")],
        schema=_Schema,
        purpose="test.schema",
    )
    assert parsed.name == "alpha"
    assert parsed.count == 7
    assert result.cost_usd == pytest.approx(result.cost_usd)


async def test_complete_json_classifies_a_schema_violation_at_the_boundary() -> None:
    """A tool input the model emitted but that fails the caller's schema is
    ``LLMMalformedOutputError`` (#1066): one typed family for "the model
    answered badly", never a raw ``ValidationError`` for an ``except
    ValueError`` downstream to turn into a 422 carrying model content."""
    client = MockLLMClient(
        scripted={"test.schema": json.dumps({"name": "alpha", "count": "seven"})}
    )
    with pytest.raises(LLMMalformedOutputError) as excinfo:
        await complete_json(
            client,
            model="claude-haiku-4-5",
            system="sys",
            messages=[Message(role="user", content="ignored")],
            schema=_Schema,
            purpose="test.schema",
        )
    exc = excinfo.value
    assert exc.reason == "schema_violation"
    assert isinstance(exc.__cause__, ValidationError)
    assert "_Schema" in exc.diagnostic
    assert "test.schema" in exc.diagnostic
    assert not isinstance(exc, ValueError)
    assert "seven" not in exc.user_message
