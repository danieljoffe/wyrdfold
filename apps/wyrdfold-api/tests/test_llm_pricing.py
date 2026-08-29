"""Cost calculation: the provider-reported figure first, the static table
only as a fallback (#933)."""

import math

import pytest

from app.models.llm import LLMUsage
from app.services.llm.pricing import PRICING, calculate_cost, reported_cost_usd, resolve_cost


def test_pricing_defined_for_every_model() -> None:
    for model in ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"):
        assert model in PRICING


def test_sonnet_input_only() -> None:
    cost = calculate_cost(
        "claude-sonnet-4-6",
        LLMUsage(input_tokens=1_000_000, output_tokens=0),
    )
    assert cost == pytest.approx(3.0, rel=1e-6)


def test_sonnet_output_only() -> None:
    cost = calculate_cost(
        "claude-sonnet-4-6",
        LLMUsage(input_tokens=0, output_tokens=1_000_000),
    )
    assert cost == pytest.approx(15.0, rel=1e-6)


def test_sonnet_mixed() -> None:
    cost = calculate_cost(
        "claude-sonnet-4-6",
        LLMUsage(input_tokens=2000, output_tokens=500),
    )
    expected = (3.0 * 2000 + 15.0 * 500) / 1_000_000
    assert cost == pytest.approx(expected, rel=1e-6)


def test_haiku_cheaper_than_sonnet_on_identical_usage() -> None:
    usage = LLMUsage(input_tokens=10_000, output_tokens=1_000)
    assert calculate_cost("claude-haiku-4-5", usage) < calculate_cost("claude-sonnet-4-6", usage)


def test_cache_read_is_one_tenth_of_input() -> None:
    full_input = calculate_cost("claude-sonnet-4-6", LLMUsage(input_tokens=1_000_000))
    cache_read = calculate_cost("claude-sonnet-4-6", LLMUsage(cache_read_input_tokens=1_000_000))
    assert cache_read == pytest.approx(full_input * 0.1, rel=1e-6)


def test_cache_write_is_1_25x_of_input() -> None:
    full_input = calculate_cost("claude-sonnet-4-6", LLMUsage(input_tokens=1_000_000))
    cache_write = calculate_cost(
        "claude-sonnet-4-6", LLMUsage(cache_creation_input_tokens=1_000_000)
    )
    assert cache_write == pytest.approx(full_input * 1.25, rel=1e-6)


def test_zero_usage_costs_nothing() -> None:
    assert calculate_cost("claude-opus-4-7", LLMUsage()) == 0.0


def test_result_rounded_to_six_decimals() -> None:
    cost = calculate_cost("claude-haiku-4-5", LLMUsage(input_tokens=37))
    assert cost == round(cost, 6)


# ---- fallback table corrections (#933) --------------------------------------
#
# These are the rates our ACTUAL route bills, not vendor list prices. The old
# entries encoded the cheap end of an unpinned route and so under-read spend,
# which loosens every gate fed by `llm_costs`.


def test_deepseek_fallback_reflects_measured_effective_billing() -> None:
    """0.27/0.40 was roughly the cheapest of 14 endpoints. Measured effective
    billing under unpinned routing was ~2.9x that, so the fallback must sit
    near the measured rate — well above the old cheap-end guess."""
    p = PRICING["deepseek-v3-2"]
    assert p.input == pytest.approx(0.83)
    assert p.output == pytest.approx(1.10)
    # The specific failure being prevented: the old cheap-end numbers.
    assert p.input > 0.27
    assert p.output > 0.40


def test_haiku_fallback_reflects_the_openrouter_routed_rate() -> None:
    """A measured OpenRouter call routed to Bedrock billed 1.00/5.00 — 1.25x
    Anthropic's 0.80/4.00 list. Prod runs LLM_PROVIDER=openrouter, so the
    routed rate is the honest fallback."""
    p = PRICING["claude-haiku-4-5"]
    assert p.input == pytest.approx(1.00)
    assert p.output == pytest.approx(5.00)
    assert p.input == pytest.approx(0.80 * 1.25)
    assert p.output == pytest.approx(4.00 * 1.25)


# ---- reported_cost_usd / resolve_cost ---------------------------------------
#
# Shape taken from a live OpenRouter response, not invented:
#   "usage": {..., "cost": 9.2694e-05, "is_byok": false,
#             "cost_details": {"upstream_inference_cost": 9.2694e-05, ...}}


def _live_shape(cost: object) -> dict[str, object]:
    return {
        "prompt_tokens": 306,
        "completion_tokens": 32,
        "cost": cost,
        "is_byok": False,
        "cost_details": {"upstream_inference_cost": cost},
    }


def test_reported_cost_is_preferred_over_the_table() -> None:
    payload = _live_shape(0.0042)
    usage = LLMUsage(input_tokens=306, output_tokens=32)

    # Anti-vacuous preconditions: a cost really IS present, and the table
    # really would produce a different number — so "reported" can only be
    # reached by reading the payload.
    assert payload["cost"] == 0.0042
    assert calculate_cost("deepseek-v3-2", usage) != pytest.approx(0.0042)

    cost, source = resolve_cost("deepseek-v3-2", usage, reported=payload)
    assert source == "reported"
    assert cost == pytest.approx(0.0042)


def test_table_is_used_when_no_cost_was_reported() -> None:
    payload: dict[str, object] = {"prompt_tokens": 306, "completion_tokens": 32}
    usage = LLMUsage(input_tokens=306, output_tokens=32)

    # Anti-vacuous precondition: the cost really IS absent.
    assert "cost" not in payload

    cost, source = resolve_cost("deepseek-v3-2", usage, reported=payload)
    assert source == "estimated"
    assert cost == pytest.approx(calculate_cost("deepseek-v3-2", usage))


def test_no_usage_payload_at_all_falls_back() -> None:
    usage = LLMUsage(input_tokens=100, output_tokens=10)
    cost, source = resolve_cost("claude-haiku-4-5", usage, reported=None)
    assert source == "estimated"
    assert cost == pytest.approx(calculate_cost("claude-haiku-4-5", usage))


def test_reported_zero_is_a_reported_cost_not_an_absent_one() -> None:
    """A free endpoint or a served cache hit bills 0.00. Treating that as
    "missing" would silently substitute a table estimate for a call that cost
    nothing — the classic `if not cost` bug."""
    usage = LLMUsage(input_tokens=1000, output_tokens=100)
    assert calculate_cost("deepseek-v3-2", usage) > 0  # precondition: table is non-zero

    cost, source = resolve_cost("deepseek-v3-2", usage, reported=_live_shape(0.0))
    assert source == "reported"
    assert cost == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        None,  # provider sent an explicit null
        "0.0042",  # a string, not a number
        True,  # bool is an int subclass — must not slip through
        -0.5,  # negative
        float("nan"),
        float("inf"),
    ],
)
def test_unusable_reported_cost_falls_back_to_the_table(bad: object) -> None:
    """Garbage in `cost` must not reach a spend gate as a real figure."""
    usage = LLMUsage(input_tokens=1000, output_tokens=100)
    cost, source = resolve_cost("deepseek-v3-2", usage, reported=_live_shape(bad))
    assert source == "estimated"
    assert cost == pytest.approx(calculate_cost("deepseek-v3-2", usage))


def test_reported_cost_is_not_rounded_away() -> None:
    """Sub-cent charges are the norm; rounding each to 6dp biases the total."""
    assert reported_cost_usd(_live_shape(9.2694e-05)) == 9.2694e-05


def test_byok_uses_upstream_inference_cost_not_the_fee() -> None:
    """For a BYOK request `cost` is only OpenRouter's percentage fee, so using
    it would understate by far more than the bug this fixes.
    `cost_details.upstream_inference_cost` is the real charge."""
    payload = {
        "cost": 0.00005,  # the fee
        "is_byok": True,
        "cost_details": {"upstream_inference_cost": 0.001},  # the real spend
    }
    usage = LLMUsage(input_tokens=1000, output_tokens=100)

    cost, source = resolve_cost("deepseek-v3-2", usage, reported=payload)
    assert source == "reported"
    assert cost == pytest.approx(0.001)
    assert cost != pytest.approx(0.00005)


def test_byok_without_upstream_detail_falls_back_to_the_table() -> None:
    usage = LLMUsage(input_tokens=1000, output_tokens=100)
    cost, source = resolve_cost("deepseek-v3-2", usage, reported={"cost": 0.00005, "is_byok": True})
    assert source == "estimated"
    assert cost == pytest.approx(calculate_cost("deepseek-v3-2", usage))


def test_reported_cost_usd_rejects_a_non_mapping() -> None:
    assert reported_cost_usd(None) is None
    assert not math.isnan(calculate_cost("deepseek-v3-2", LLMUsage(input_tokens=1)))
