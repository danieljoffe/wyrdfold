# OpenRouter `/api/v1/messages` recorded corpus (#1067)

Recorded 2026-09-18 from the workstation against `anthropic/claude-sonnet-4.6` with
`x-api-key` + `anthropic-version: 2023-06-01`. These are the wire responses the raw
Messages transport must parse; the SDK fakes in `tests/support/sdk_fakes.py` cannot
stand in for them because the raw path never sees SDK objects.

What was verified live (also on #1067):

- `x-api-key` works with and without `anthropic-version`; `Authorization: Bearer` works too.
- Non-stream responses are Anthropic-shaped plus extras (`provider`, `stop_details`,
  `context_management`, `container`; `usage.cost`, `usage.cost_details`, `usage.is_byok`,
  `usage.cache_creation`, `usage.speed`, ...).
- A `$defs` schema passes through untouched and yields a `tool_use` block.
- Streams: `message_start` (usage without cost) ... `message_delta` (usage WITH cost),
  `message_stop`, then an OpenAI-style trailer `event: data` / `data: [DONE]`.
- Cache pair: create run `cache_creation_input_tokens=2403`, read run
  `cache_read_input_tokens=2403`, both reported on `message_delta`.
- Bad requests are real HTTP 400s with `{"type":"error","error":{...}}` bodies.

**Cost values are placeholders.** Every `cost` / `upstream_inference_*` number was replaced
with a fixed fake (`0.001234` / `0.001` / `0.000234`) so the public repo carries no real
per-call prices. Token counts, ids, and structure are verbatim. The long cacheable system
prompt in the stream requests is elided in `*.request.json` (its length is noted).
