"""Tiny OpenRouter client + env-loader shared by the eval scripts.

Not a service module (lives under scripts/, not app/) — exists only to
keep multi_model_judge.py and openrouter_smoke.py readable.

Uses httpx directly against the OpenAI-compatible /chat/completions
endpoint so we get one code path across Anthropic / OpenAI / Google /
DeepSeek models without a per-provider SDK.

Env: reads OPENROUTER_API_KEY — the SAME name the app's settings use, so
one credential has one spelling. Falls back to ``apps/wyrdfold-api/.env.local``
and then ~/.zshrc. Don't print the key.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

_OR_BASE = "https://openrouter.ai/api/v1"

# The app reads ``OPENROUTER_API_KEY`` (``Settings.openrouter_api_key``). This
# harness used to read ``OPEN_ROUTER_API_KEY`` — one credential, two spellings.
# The cost was real: the only live key lived in ``.env.local`` under the app's
# name while ~/.zshrc held a dead key under the harness's name, so `source
# ~/.zshrc` ACTIVELY SET the broken one and every eval 401'd. #780 sat blocked
# on it. The legacy name is still accepted so an old shell keeps working, but
# it is no longer what anything is documented to use.
_KEY_ENV = "OPENROUTER_API_KEY"
_LEGACY_KEY_ENV = "OPEN_ROUTER_API_KEY"


def _from_env_file(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        m = re.match(rf'\s*(?:export\s+)?{name}=["\']?([^"\'\s]+)', line)
        if m:
            return m.group(1)
    return None


# Module-level so tests can point them at fixtures instead of the real files.
ENV_LOCAL = Path(__file__).resolve().parents[1] / ".env.local"
ZSHRC = Path.home() / ".zshrc"


def get_api_key() -> str:
    """Resolve the OpenRouter API key.

    The CANONICAL name is tried across every source before the legacy name is
    tried anywhere. That ordering is the fix, not an accident: the original
    failure was a stale ``OPEN_ROUTER_API_KEY`` in the shell shadowing the live
    ``OPENROUTER_API_KEY`` sitting in ``.env.local``. Per-source precedence
    would reproduce it exactly.

    Sources, in order: process env, the API's own ``.env.local`` (the file the
    app itself reads, so harness and prod agree by construction), then ~/.zshrc.
    """
    for name in (_KEY_ENV, _LEGACY_KEY_ENV):
        key = (
            os.environ.get(name)
            or _from_env_file(ENV_LOCAL, name)
            or _from_env_file(ZSHRC, name)
        )
        if key:
            return key
    raise RuntimeError(
        f"{_KEY_ENV} not set. Export it, or add it to {ENV_LOCAL} or ~/.zshrc. "
        f"(The legacy name {_LEGACY_KEY_ENV} is still accepted.)"
    )


# Canonical model slugs the eval harness uses. OpenRouter routes the
# slug to a provider; the model itself is identical across providers
# (e.g. anthropic/claude-sonnet-4-6 is the same weights whether served
# by Anthropic-direct or via Bedrock).
MODELS: dict[str, str] = {
    # Current production baseline.
    "sonnet-4.6": "anthropic/claude-sonnet-4.6",
    # Previous Sonnet — same family, often cheaper / quota-spillover target.
    "sonnet-4.5": "anthropic/claude-sonnet-4.5",
    # Cross-provider flagships.
    "gpt-5.1": "openai/gpt-5.1",
    "gemini-2.5-pro": "google/gemini-2.5-pro",
    # Open-source flagship — biggest price delta vs Sonnet.
    "deepseek-v3.2": "deepseek/deepseek-v3.2",
}


@dataclass
class CallResult:
    """One OpenRouter call's outcome.

    `parsed` is the JSON object the model returned (or None on parse
    failure). `raw_content` is the literal model output, kept for
    debugging when parsing fails. `usage` is whatever OpenRouter
    reported, normalized into a flat dict.
    """

    model_slug: str
    parsed: dict[str, Any] | None
    raw_content: str
    usage: dict[str, int]
    latency_ms: int
    cost_usd: float
    error: str | None = None


def _normalize_usage(usage_raw: dict[str, Any]) -> dict[str, int]:
    """Pull the consistent fields out of OpenRouter's usage dict."""
    return {
        "prompt_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "completion_tokens": int(usage_raw.get("completion_tokens", 0)),
        "total_tokens": int(usage_raw.get("total_tokens", 0)),
    }


def _extract_cost(payload: dict[str, Any]) -> float:
    """OpenRouter includes `usage.cost` in USD when usage tracking is on."""
    usage = payload.get("usage") or {}
    return float(usage.get("cost", 0.0) or 0.0)


def _try_parse_json(content: str) -> dict[str, Any] | None:
    """Be generous: some models wrap in ```json … ```, some emit prose
    before/after the object, some get the JSON right. Find the first
    `{` and the last `}` and try that. Return None on failure (caller
    treats as a soft error so one bad model doesn't kill the whole run).
    """
    if not content:
        return None
    s = content.strip()
    # Strip leading ```json / ``` markers.
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Bracket-walking fallback.
    first = s.find("{")
    last = s.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None
    try:
        return json.loads(s[first : last + 1])
    except json.JSONDecodeError:
        return None


async def call_model(
    *,
    model_slug: str,
    system: str,
    user: str,
    api_key: str | None = None,
    max_tokens: int = 1024,
    request_timeout_seconds: float = 120.0,
    response_format_json: bool = True,
    temperature: float | None = None,
) -> CallResult:
    """Single OpenRouter /chat/completions call.

    ``response_format_json=True`` asks for JSON mode. Anthropic ignores
    it gracefully; OpenAI/Gemini honor it; some open-source models
    don't. Either way we attempt to parse JSON from the returned text.

    ``temperature`` defaults to None, which leaves it unset and lets the
    provider pick (~1.0 for Anthropic). That is NOT what production does:
    ``app/services/llm/client.py:complete_json`` defaults it to **0.0** and
    forwards it, so every scoring/extraction call in the app runs at 0. An
    eval that leaves it unset therefore measures a noisier distribution than
    the thing it is meant to be measuring — pass ``temperature=0.0`` to match
    prod. Left as None rather than defaulting to 0 here so the existing
    bake-off scripts, which deliberately sample, are unaffected.
    """
    key = api_key or get_api_key()
    body: dict[str, Any] = {
        "model": model_slug,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "usage": {"include": True},  # ask OpenRouter to report cost
    }
    if temperature is not None:
        body["temperature"] = temperature
    if response_format_json:
        body["response_format"] = {"type": "json_object"}

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=request_timeout_seconds) as http:
            resp = await http.post(
                f"{_OR_BASE}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    # Optional but helps OpenRouter dashboard analytics.
                    "HTTP-Referer": "https://danieljoffe.com",
                    "X-Title": "wyrdfold-eval",
                },
            )
        latency_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code != 200:
            return CallResult(
                model_slug=model_slug,
                parsed=None,
                raw_content="",
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                latency_ms=latency_ms,
                cost_usd=0.0,
                error=f"HTTP {resp.status_code}: {resp.text[:300]}",
            )
        payload = resp.json()
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return CallResult(
            model_slug=model_slug,
            parsed=None,
            raw_content="",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            latency_ms=latency_ms,
            cost_usd=0.0,
            error=f"{type(exc).__name__}: {exc}",
        )

    content = ""
    choices = payload.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""

    return CallResult(
        model_slug=model_slug,
        parsed=_try_parse_json(content),
        raw_content=content,
        usage=_normalize_usage(payload.get("usage") or {}),
        latency_ms=latency_ms,
        cost_usd=_extract_cost(payload),
        error=None,
    )
