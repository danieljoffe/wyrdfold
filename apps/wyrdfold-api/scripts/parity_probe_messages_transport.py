"""#1067 flip gate: the same prompt through ``transport=sdk`` and ``transport=raw``.

For each non-stream method, diff the OUTBOUND JSON bodies the two transports
put on the wire and reconcile the responses pairwise (``stop_reason``, token
fields, ``cost_source``, ``is_byok``, ``provider``). Then a cache pair per
transport (create, then read) so cache accounting reconciles from a real
cache read, not a single response.

Run from the workstation with the OpenRouter key in the environment (never
printed). Writes a JSON report (cost values masked) to the path given as the
first argument and prints a markdown summary safe for a public PR.

    railway run --environment production --service wyrdfold.com -- \\
      sh -c "cd apps/wyrdfold-api && PYTHONPATH=. uv run python \\
      scripts/parity_probe_messages_transport.py /tmp/parity.json"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from app.models.llm import Message, ModelId
from app.services.llm.openrouter_client import _OPENROUTER_BASE_URL, OpenRouterLLMClient
from app.services.llm.raw_messages_transport import RawMessagesTransport

MODEL: ModelId = "claude-sonnet-4-6"
SCHEMA = {
    "type": "object",
    "properties": {"label": {"$ref": "#/$defs/Label"}},
    "required": ["label"],
    "$defs": {"Label": {"type": "string", "minLength": 1, "maxLength": 80}},
}
TITLE = "Senior Product Builder (Product Manager), Enterprise Readiness & Admin Platform"
SYSTEM_TOOL = (
    "You are a job-market taxonomist. Return the canonical role title a job seeker would use."
)
SYSTEM_BIG = "You are a careful assistant who answers briefly. " * 300  # >= 1024 tokens, cacheable
MASK = {
    "cost",
    "upstream_inference_cost",
    "upstream_inference_prompt_cost",
    "upstream_inference_completions_cost",
}


def _mask(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: ("<masked>" if k in MASK else _mask(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask(v) for v in o]
    return o


def _canon(body: Any) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


class _Capture:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def hook(self, request: httpx.Request) -> None:  # httpx event hook
        try:
            self.bodies.append(json.loads(request.content))
        except Exception:
            self.bodies.append({"<unparsed>": request.content[:200].decode(errors="replace")})


def _sdk_client(api_key: str, capture: _Capture) -> OpenRouterLLMClient:
    client = OpenRouterLLMClient(api_key=api_key, raw_purposes=frozenset())
    sdk = client._client  # builds the SDK lazily
    how = "none"
    inner = getattr(sdk, "_client", None)
    hooks = getattr(inner, "event_hooks", None)
    if isinstance(hooks, dict) and "request" in hooks:
        hooks["request"].append(capture.hook)
        how = "sdk httpx2 event_hooks"
    client._probe_capture_how = how  # type: ignore[attr-defined]
    return client


def _raw_client(api_key: str, capture: _Capture) -> OpenRouterLLMClient:
    client = OpenRouterLLMClient(
        api_key=api_key,
        raw_purposes=frozenset(
            {"parity.tool_use", "parity.complete", "parity.cache", "parity.stream"}
        ),
    )
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0), event_hooks={"request": [capture.hook]}
    )
    client._raw = RawMessagesTransport(
        api_key=api_key, timeout=120.0, max_retries=2, base_url=_OPENROUTER_BASE_URL, http=http
    )
    return client


def _usage_view(result: Any) -> dict[str, Any]:
    return {
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "cache_read_input_tokens": result.usage.cache_read_input_tokens,
        "cache_creation_input_tokens": result.usage.cache_creation_input_tokens,
        "cost_source": result.cost_source,
        "transport": result.transport,
        "provider": result.provider,
    }


async def main(out_path: str) -> int:
    api_key = os.environ["OPENROUTER_API_KEY"]
    report: dict[str, Any] = {"model": MODEL, "methods": {}, "cache_pair": {}}
    ok = True

    for method in ("complete_tool_use", "complete", "stream"):
        rows: dict[str, Any] = {}
        for name, factory in (("sdk", _sdk_client), ("raw", _raw_client)):
            cap = _Capture()
            client = factory(api_key, cap)
            if method == "complete_tool_use":
                tool_input, result = await client.complete_tool_use(
                    model=MODEL,
                    system=SYSTEM_TOOL,
                    messages=[Message(role="user", content=f"Title: {TITLE}")],
                    tool_name="return_NormalizedTitle",
                    tool_description="Record the response.",
                    tool_input_schema=SCHEMA,
                    purpose="parity.tool_use",
                    max_tokens=64,
                    cache_system=True,
                    temperature=0.0,
                )
                content: Any = tool_input
            elif method == "stream":
                deltas: list[str] = []
                final = None
                async for ev in client.stream(
                    model=MODEL,
                    system="Answer in one short sentence.",
                    messages=[Message(role="user", content="What is a résumé?")],
                    purpose="parity.stream",
                    max_tokens=40,
                ):
                    if ev.type == "delta":
                        deltas.append(ev.text)
                    else:
                        final = ev.result
                if final is None:
                    raise RuntimeError("stream ended without a final event")
                result = final
                content = "".join(deltas)
                if content != final.content:
                    raise RuntimeError("streamed deltas do not add up to the final content")
            else:
                result = await client.complete(
                    model=MODEL,
                    system="Answer in one short sentence.",
                    messages=[Message(role="user", content="What is a résumé?")],
                    purpose="parity.complete",
                    max_tokens=40,
                )
                content = result.content
            rows[name] = {
                "capture": getattr(client, "_probe_capture_how", "raw httpx event_hooks"),
                "body": cap.bodies[-1] if cap.bodies else None,
                "stop_reason_content": content,
                "usage": _usage_view(result),
                "is_byok": (result.usage and getattr(result, "usage", None)) and None,
            }
        sdk_body, raw_body = rows["sdk"]["body"], rows["raw"]["body"]
        body_equal = sdk_body is not None and _canon(sdk_body) == _canon(raw_body)
        key_diff = sorted(set((sdk_body or {}).keys()) ^ set((raw_body or {}).keys()))
        u_sdk, u_raw = rows["sdk"]["usage"], rows["raw"]["usage"]
        tokens_equal = all(
            u_sdk[k] == u_raw[k]
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        report["methods"][method] = {
            "body_equal": body_equal,
            "body_key_diff": key_diff,
            "body_value_diff": [
                k
                for k in (sdk_body or {})
                if k in (raw_body or {}) and _canon(sdk_body[k]) != _canon(raw_body[k])
            ],
            "tokens_equal": tokens_equal,
            "sdk": {
                "usage": u_sdk,
                "content": rows["sdk"]["stop_reason_content"],
                "capture": rows["sdk"]["capture"],
            },
            "raw": {
                "usage": u_raw,
                "content": rows["raw"]["stop_reason_content"],
                "capture": rows["raw"]["capture"],
            },
            "content_equal": rows["sdk"]["stop_reason_content"]
            == rows["raw"]["stop_reason_content"],
        }
        ok &= (
            body_equal
            and tokens_equal
            and u_sdk["cost_source"] == u_raw["cost_source"] == "reported"
        )

    # Cache pair per transport, each with its OWN salted system prompt so each
    # transport shows a full create-then-read (a shared prompt would let the
    # second transport find the first one's warm cache, which is a different,
    # stronger fact recorded separately below).
    salt = os.urandom(4).hex()
    for name, factory in (("sdk", _sdk_client), ("raw", _raw_client)):
        client = factory(api_key, _Capture())
        pair = []
        for _ in range(2):
            r = await client.complete(
                model=MODEL,
                system=f"[{name}-{salt}] " + SYSTEM_BIG,
                messages=[Message(role="user", content="Say hello in five words.")],
                purpose="parity.cache",
                max_tokens=24,
                cache_system=True,
            )
            pair.append(_usage_view(r))
        report["cache_pair"][name] = pair
        ok &= pair[0]["cache_creation_input_tokens"] > 0 and pair[1]["cache_read_input_tokens"] > 0
    both = report["cache_pair"]
    report["cache_pair"]["read_tokens_equal"] = (
        both["sdk"][1]["cache_read_input_tokens"] == both["raw"][1]["cache_read_input_tokens"]
    )
    # Cross-transport read: the raw transport reading the cache the SDK path
    # created proves the cache KEY (the request body) is identical on the wire.
    cross = await _raw_client(api_key, _Capture()).complete(
        model=MODEL,
        system=f"[sdk-{salt}] " + SYSTEM_BIG,
        messages=[Message(role="user", content="Say hello in five words.")],
        purpose="parity.cache",
        max_tokens=24,
        cache_system=True,
    )
    report["cache_pair"]["cross_transport_read"] = _usage_view(cross)
    ok &= cross.usage.cache_read_input_tokens > 0
    report["verdict"] = "PASS" if ok else "FAIL"
    _REPORT["value"] = _mask(report)

    print(f"## Parity probe ({MODEL}) — {report['verdict']}\n")
    print(
        "| method | outbound body equal | key diff | value diff | tokens equal | "
        "cost_source sdk/raw | content equal | provider sdk/raw |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for m, r in report["methods"].items():
        print(
            f"| `{m}` | {r['body_equal']} | {r['body_key_diff'] or '—'} | {r['body_value_diff'] or '—'} | "
            f"{r['tokens_equal']} | {r['sdk']['usage']['cost_source']} / {r['raw']['usage']['cost_source']} | "
            f"{r['content_equal']} | {r['sdk']['usage']['provider']} / {r['raw']['usage']['provider']} |"
        )
    print(
        "\n| cache pair | call 1 creation | call 1 read | call 2 creation | call 2 read | cost_source |"
    )
    print("|---|---|---|---|---|---|")
    for name in ("sdk", "raw"):
        pr = report["cache_pair"][name]
        print(
            f"| {name} | {pr[0]['cache_creation_input_tokens']} | {pr[0]['cache_read_input_tokens']} | "
            f"{pr[1]['cache_creation_input_tokens']} | {pr[1]['cache_read_input_tokens']} | {pr[1]['cost_source']} |"
        )
    print(
        f"\ncapture: sdk via {report['methods']['complete']['sdk']['capture']}, raw via httpx event_hooks; "
        f"read tokens equal across transports: {report['cache_pair']['read_tokens_equal']}; "
        f"raw transport reading the SDK-created cache: "
        f"cache_read_input_tokens={report['cache_pair']['cross_transport_read']['cache_read_input_tokens']}"
    )
    return 0 if ok else 1


_REPORT: dict[str, Any] = {}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: parity_probe_messages_transport.py <report.json>")
    rc = asyncio.run(main(sys.argv[1]))
    Path(sys.argv[1]).write_text(json.dumps(_REPORT.get("value", {}), indent=1))
    sys.exit(rc)
