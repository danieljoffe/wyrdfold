---
paths:
  - 'apps/wyrdfold-api/app/services/llm/**'
  - 'apps/wyrdfold-api/app/services/relevance/**'
  - 'apps/wyrdfold-api/app/services/qualification/**'
  - 'apps/wyrdfold-api/app/services/fit/**'
  - 'apps/wyrdfold-api/app/services/analysis/**'
  - 'apps/wyrdfold-api/app/services/tailor/**'
  - 'apps/wyrdfold-api/app/services/conversation/**'
  - 'apps/wyrdfold-api/app/services/llm_learner.py'
  - 'apps/wyrdfold-api/app/services/jd_parser.py'
  - 'apps/wyrdfold-api/tests/test_llm_mock.py'
---

# Grow the LLM mock with every PR that touches LLM surfaces

A PR touching LLM calls, prompts, or LLM-output parsing must extend the mock's
edge battery (`app/services/llm/mock.py` + `tests/test_llm_mock.py`) for the
surface it touched: malformed/truncated JSON, fenced output, schema-violating
payloads, empty content, injection-looking text echoed as data, mid-stream
provider errors, …

Every LLM bug we hit becomes a named mock behavior + regression test — the mock
is the accumulated bug corpus, so new endpoints inherit every past failure mode
for free. See `CONTRIBUTING.md` → "Touching prompts or scoring code".
