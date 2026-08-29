"""Pydantic models for the LLM plumbing layer (#185 P2a).

Interface-only. The real Anthropic client ships in a later phase;
P2a uses MockLLMClient so the rest of the system can be wired up and
tested without external API calls.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ModelId = Literal[
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    # Non-Anthropic, reached via OpenRouter's OpenAI-compatible endpoint
    # (see OpenRouterLLMClient's OpenAI-shaped path). DeepSeek V3.2 backs
    # Phase-1 triage when phase1_triage_model selects it.
    "deepseek-v3-2",
]
"""Models this workspace targets. Extend as new models land."""

TurnRole = Literal["user", "assistant"]


class Message(BaseModel):
    role: TurnRole
    content: str
    # Prompt-caching marker (optional, additive). When set, the first
    # ``cache_prefix_chars`` characters of ``content`` are a STATIC
    # prefix (stable across calls — e.g. the per-target example pools in
    # Phase 1, or the user-profile + target context in Phase 2). Real
    # Anthropic-shaped clients split the message into two text blocks at
    # exactly that boundary and set ``cache_control: {"type":
    # "ephemeral"}`` on the first; block concatenation is byte-identical
    # to ``content``, so the prompt the model sees is unchanged. The
    # mock client and any consumer that only reads ``content`` ignore
    # the marker entirely.
    cache_prefix_chars: int | None = Field(default=None, ge=1)


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


CostSource = Literal["reported", "estimated"]
"""Where a call's ``cost_usd`` came from.

``reported`` — the provider told us what it charged (OpenRouter returns
``usage.cost`` on every response). ``estimated`` — derived from the static
per-model table in ``app.services.llm.pricing``, which cannot represent a
routed price and is only as good as the rate we guessed. Consumers read
``cost_usd``; this exists so a reader of a cost row can tell which kind of
number they are looking at.
"""


class LLMResult(BaseModel):
    content: str
    model: ModelId
    usage: LLMUsage
    cost_usd: float
    latency_ms: int
    # Defaults to "estimated" so every existing constructor (the mock, the
    # embedding-free test fixtures) stays valid; the real clients set it.
    cost_source: CostSource = "estimated"


class LLMStreamDelta(BaseModel):
    """Incremental text chunk from a streaming completion."""

    type: Literal["delta"] = "delta"
    text: str


class LLMStreamFinal(BaseModel):
    """Terminal event for a streaming completion. Carries the full result
    so callers can cost-log and parse a typed payload from `result.content`.
    """

    type: Literal["final"] = "final"
    result: LLMResult


LLMStreamEvent = LLMStreamDelta | LLMStreamFinal


class LLMCallRecord(BaseModel):
    """Read shape for llm_costs rows.

    Covers both LLM completions and embedding calls. The `model` column
    holds either a Claude ID (ModelId) or a Voyage ID (EmbeddingModelId);
    typed as `str` here since at read time we don't disambiguate.
    """

    id: str
    user_id: str | None
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_usd: float
    latency_ms: int
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
    created_at: datetime
