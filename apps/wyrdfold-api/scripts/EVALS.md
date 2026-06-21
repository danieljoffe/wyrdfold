# LLM evals & the recurring cadence (#27)

The product promise is match quality, so prompt/model/scoring changes need a
regression check. Real eval data is a snapshot of production résumé/job content
(**PII**) and the deepest eval needs an Anthropic key + spend — so the cadence
is tiered to keep the cheap, safe checks automatic and the expensive, sensitive
ones deliberate.

## Tier 1 — per-PR, automatic, free, PII-free

`tests/test_prompt_regression.py` pins every scoring/matching/generation system
prompt + per-purpose model + prompt-version into a golden snapshot
(`tests/golden/llm_behavior_contract.txt`). Any prompt edit, model swap, or
version bump **fails the normal Python CI job** until you re-baseline:

```bash
cd apps/wyrdfold-api
UPDATE_PROMPT_GOLDENS=1 uv run pytest tests/test_prompt_regression.py
```

This catches _that_ behaviour changed; it does not measure whether quality got
better or worse — that's tiers 2–3.

## Tier 2 — on-demand (+ optional monthly), automated, PII-free

The **`Evals — LLM matching quality`** GitHub Action
(`.github/workflows/evals.yml`) runs the schema + cross-model evals
(`eval_phase1_triage`, `eval_derive_target`) against a **fabricated** fixture
(`scripts/gen_sample_eval_set.py` — no real user data), so it never exposes PII.

- **One-time setup:** add an `OPENROUTER_API_KEY` repository secret. Without it
  the workflow skips with a warning instead of failing.
- **Run it:** Actions → _Evals — LLM matching quality_ → _Run workflow_ (choose
  `both` / `phase1` / `derive`, optional phase-1 model subset). Results land in
  the run's job summary. ~$0.5/run.
- **Make it recurring:** uncomment the `schedule:` block in the workflow.

What it catches: schema-validity regressions (e.g. the #27 derive failures) and
cross-model agreement drift on a fixed, reproducible set. What it does NOT
catch: real-data quality vs the production gold (synthetic gold).

To reproduce locally (PII-free):

```bash
cd apps/wyrdfold-api
export OPEN_ROUTER_API_KEY=sk-or-...
uv run python scripts/gen_sample_eval_set.py
uv run python scripts/eval_phase1_triage.py
uv run python scripts/eval_derive_target.py
```

## Tier 3 — real-data quality baseline, manual + LOCAL only

The grading eval (`eval_grading_prompts.py`) compares the current prompt against
the **production gold** scores and **requires `LLM_PROVIDER=anthropic`** (a
direct Anthropic key with quota — it refuses OpenRouter). It needs a real
snapshot, so it stays off CI:

```bash
cd apps/wyrdfold-api
# 1. Snapshot ~50 real cases from a Supabase you control (writes the gitignored
#    tests/fixtures/eval_set.json — résumé/job PII, delete when done):
SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  uv run python -m scripts.eval_grading_prompts --snapshot
# 2. Grade against gold (Spearman ρ / top-K overlap / per-axis RMSE):
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... \
  uv run python -m scripts.eval_grading_prompts
# 3. Purge the PII fixture:
rm tests/fixtures/eval_set.json
```

Run this when you change a scoring prompt/model and want the true
quality delta — attach the before/after summary to the PR (see
`CONTRIBUTING.md` → "Touching prompts or scoring code").

### Handling secrets

Never paste keys into chat, commits, or `eval_results/`. Use env vars (or
`~/.zshrc`, which `scripts/_openrouter.py` reads as a fallback). `eval_results/`
and the snapshot fixture are gitignored; the snapshot is real PII — delete it
after a tier-3 run.
