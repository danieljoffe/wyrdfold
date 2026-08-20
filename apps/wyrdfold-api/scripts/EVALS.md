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
export OPENROUTER_API_KEY=sk-or-...
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

## Qualification-tagger correctness — committed golden (part CI, part on-demand)

`eval_qualification_correctness.py` (#193) scores the L2 qualification tagger's
`is_us` / `role_family` against a **committed, PII-free** golden set
(`tests/fixtures/qualification_golden.json`, hand-labeled objective cases) —
correctness vs ground truth, not drift vs a past run. `is_us` positive class is
"is US", so the prod failure that motivated it — a conf-95 false NEGATIVE, an
unambiguous US location tagged non-US ("New York, NY, United States") — surfaces
as a **recall** miss.

Because the fixture is committed and PII-free, the pure metric functions + the
golden guards (incl. the conf-95 regression case) run **free in CI**
(`tests/test_eval_qualification_correctness.py`, tier-1). The model-accuracy run
is on-demand with the real LLM:

```bash
railway run uv run --package wyrdfold-api \
  python apps/wyrdfold-api/scripts/eval_qualification_correctness.py
```

Reports `is_us` accuracy / recall / precision / FNR + `role_family` accuracy, and
names each miss (title @ location → predicted, confidence). ~1 Haiku call/case.

## Faithfulness judge — committed golden (part CI, part on-demand)

`eval_faithfulness.py` (#193) validates the tailor's anti-hallucination guard
(`review_resume_faithfulness`): does it actually **catch** fabrications? Against a
committed golden set (`tests/fixtures/faithfulness_golden.json`) of (source
experience, tailored resume) pairs — half with a planted fabrication /
exaggeration / unsupported_skill, half faithful (grounded / rephrased /
grounded-metric the judge must NOT flag). Positive class = "has a hallucination",
so **recall is the catch rate** and a false negative is a MISSED fabrication (the
dangerous class); precision measures over-flagging.

Committed + PII-free, so the metric functions + fixture guards (deserializes into
the real `OptimizedPayload`/`TailoredResume`, balanced across all issue types,
each planted claim present in its resume) run **free in CI**
(`tests/test_eval_faithfulness.py`). The judge-accuracy run is on-demand:

```bash
railway run uv run --package wyrdfold-api \
  python apps/wyrdfold-api/scripts/eval_faithfulness.py
```

Reports catch_rate / precision / miss_rate and names each **missed** hallucination

- each false flag. ~1 review call/case.

## Grading correctness — committed golden, GROSS cases (part CI, part on-demand)

`eval_grading_correctness.py` (#193) turns the drift-only grading eval (above,
ρ vs the shifting production baseline) into a **correctness** one: a committed
golden set (`tests/fixtures/grading_golden.json`) of (target, resume, job)
triples where the right fit is UNAMBIGUOUS — a warehouse job vs a senior-frontend
target+resume must land LOW, a matching job HIGH — with WIDE bands (high≥50,
low≤25). It catches GROSS regressions (an off-domain job scoring 60 for a
specific target) without over-specifying a subtle score. Reuses the real grader
(`_grade_one` → `job_fit`).

CI-free part: pure band metrics + the fixture guard that it deserializes into the
real `JobTarget`/`OptimizedPayload` (`tests/test_eval_grading_correctness.py`).
Grader run is on-demand:

```bash
railway run uv run --package wyrdfold-api \
  python apps/wyrdfold-api/scripts/eval_grading_correctness.py
```

Bands **ratified 2026-07-07** against the live grader (Sonnet-4.6): 100% band
accuracy (6/6) — highs scored 72/92/95, lows 2/4/4 (a clean ~68-pt gap).

### Handling secrets

Never paste keys into chat, commits, or `eval_results/`. Use env vars (or
`~/.zshrc`, which `scripts/_openrouter.py` reads as a fallback). `eval_results/`
and the snapshot fixture are gitignored; the snapshot is real PII — delete it
after a tier-3 run.
