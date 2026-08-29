"""What a single LLM call cost, and where that number came from.

There are two sources, and the order matters (#933):

1. **What the provider says it charged.** OpenRouter returns ``usage.cost``
   on *every* response — no opt-in parameter, the old ``usage: {include:
   true}`` flag is deprecated and has no effect. That figure is what our
   account was actually billed, so it is the truth and it wins.
2. **The static table below**, used only when (1) is missing — a direct
   Anthropic call, or a response that somehow omitted the field.

Why (1) exists at all: we send OpenRouter slugs **unpinned**, and OpenRouter
serves ``deepseek/deepseek-v3.2`` from 14 endpoints spanning $0.209/$0.310 to
$3.00/$4.50 per Mtok — a 14x spread. **Price is a routing outcome, not a
property of the model**, so no static table can be right. It is not merely a
reporting nicety: ``llm_costs`` feeds ``_global_budget_exhausted``,
``PayerBudgetGate``, ``grading_budget_reserve_usd`` and ``check_daily_count``,
so an under-read there lets every one of those gates pass proportionally more
traffic than configured.

Units, verified against a live OpenRouter response and the docs: ``usage.cost``
is **USD**. OpenRouter's credit system uses US dollars as its base currency and
the docs describe the field as "the total amount charged to your account" — no
scaling. It is also **already net of prompt caching**: a measured Haiku
cache-write call billed 6,001 tokens at 1.25x the input rate and the following
cache-read call billed the same 6,001 tokens at 0.1x, both matching ``cost``
exactly.

Static-table prices are USD per million tokens. Cache multipliers follow
Anthropic's standard: writes cost 1.25x the base input rate, reads 0.1x.
"""

import math
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.models.llm import CostSource, LLMUsage, ModelId


class ModelPricing(BaseModel):
    """USD per million tokens."""

    input: float
    output: float

    @property
    def cache_read(self) -> float:
        return self.input * 0.1

    @property
    def cache_write(self) -> float:
        return self.input * 1.25


# FALLBACK rates only — reached when the provider did not report a cost.
#
# These are deliberately the rates our ACTUAL route bills, not vendor list
# prices, because prod runs ``LLM_PROVIDER=openrouter``. Where the two differ
# we take the higher one on purpose: over-estimating a fallback trips a spend
# gate early (visible, safe), while under-estimating loosens every gate
# silently — the failure #933 was about.
PRICING: dict[ModelId, ModelPricing] = {
    "claude-opus-4-7": ModelPricing(input=15.00, output=75.00),
    "claude-sonnet-4-6": ModelPricing(input=3.00, output=15.00),
    # Anthropic's own list price is 0.80/4.00, but a measured OpenRouter call
    # routed to Amazon Bedrock billed 1.00/5.00 — exactly the ~1.25x under-read
    # #933 reports. A direct-Anthropic call therefore over-estimates by 25%
    # here; that is the safe direction, and it only applies when the response
    # carried no cost at all.
    "claude-haiku-4-5": ModelPricing(input=1.00, output=5.00),
    # DeepSeek V3.2 via OpenRouter, UNPINNED. The old 0.27/0.40 encoded roughly
    # the cheapest endpoint's list price; the bake-off measured effective
    # billing at ~0.83/1.10 across a real run, ~2.9x higher, because routing
    # lands on whichever of the 14 endpoints is available. That measured
    # effective rate is the honest fallback. The 0.1x cache-read ratio is
    # Anthropic's convention, kept for internal consistency.
    "deepseek-v3-2": ModelPricing(input=0.83, output=1.10),
}


def calculate_cost(model: ModelId, usage: LLMUsage) -> float:
    """Estimated USD cost of a single call from the static table.

    A guess. Prefer :func:`resolve_cost`, which uses this only when the
    provider reported nothing.
    """
    p = PRICING[model]
    total_per_mtok = (
        p.input * usage.input_tokens
        + p.output * usage.output_tokens
        + p.cache_read * usage.cache_read_input_tokens
        + p.cache_write * usage.cache_creation_input_tokens
    )
    return round(total_per_mtok / 1_000_000, 6)


def reported_cost_usd(usage_payload: Mapping[str, Any] | None) -> float | None:
    """The USD the provider says it charged for one call, or ``None``.

    ``usage_payload`` is the provider's raw ``usage`` object: the JSON dict on
    the OpenAI-shaped ``/chat/completions`` response, or the *extra* fields
    the Anthropic SDK could not type on an Anthropic-shaped ``/v1/messages``
    response (see ``anthropic_client._reported_usage``). Both carry the same
    ``cost`` / ``cost_details`` / ``is_byok`` keys — measured, not assumed.

    ``None`` means "nothing usable here, fall back to the table". Anything
    that is not a finite non-negative real number is treated that way, so a
    provider sending ``null``, a string, or a bool cannot poison a spend gate
    with a nonsense figure. A genuine ``0.0`` (a free endpoint, a served cache
    hit) IS a reported cost and is returned as such — it must not be confused
    with "absent".

    The value is returned unrounded: it is the exact amount billed, and
    rounding many sub-cent charges to 6dp would bias the total.
    """
    if not isinstance(usage_payload, Mapping):
        return None

    cost: Any = usage_payload.get("cost")
    if usage_payload.get("is_byok"):
        # BYOK (provider keys registered with OpenRouter): ``cost`` is only
        # OpenRouter's percentage fee, not the inference spend, so using it
        # would understate by far more than the bug we are fixing.
        # ``cost_details.upstream_inference_cost`` is the real charge.
        details = usage_payload.get("cost_details")
        cost = details.get("upstream_inference_cost") if isinstance(details, Mapping) else None

    # bool is an int subclass — exclude it explicitly.
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    value = float(cost)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def resolve_cost(
    model: ModelId,
    usage: LLMUsage,
    *,
    reported: Mapping[str, Any] | None,
) -> tuple[float, CostSource]:
    """``(cost_usd, provenance)`` for one call — reported if we have it.

    The single funnel every client uses, so "prefer what we were billed" is
    one decision in one place rather than four call sites that can drift.
    """
    value = reported_cost_usd(reported)
    if value is not None:
        return value, "reported"
    return calculate_cost(model, usage), "estimated"
