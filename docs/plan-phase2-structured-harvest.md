# Plan: Phase-2 structured harvest — get the tax's worth out of every graded read

Status: **proposed** — needs the owner's go on ONE spend-bearing eval
re-baseline before any build. The free-mining slice (near-miss insights) shipped
separately and needed no gate.
Context: 2026-08-13, follow-up to `plan-phase1-rejection-persistence.md`.

## Premise

Every grading call is a paid comprehension of a listing; most calls return a
bit or a few enums and discard the rest of the comprehension. Extract durable,
queryable facts **in the same calls** — enrich the catalog per read instead of
paying again later.

## What each phase reads vs. keeps (inventory, 2026-08-13)

| Phase          | Reads                                | Keeps                                                                               | Discards                                         |
| -------------- | ------------------------------------ | ----------------------------------------------------------------------------------- | ------------------------------------------------ |
| P1 triage      | title only, high volume              | promising + confidence (persisted since #703)                                       | nothing worth keeping — titles are thin          |
| Tagger         | 600-char snippet, once/job           | is_us(+conf), role_family, seniority, employment_type, metro, is_remote, is_genuine | little                                           |
| **P2 fit.job** | **full JD + target profile**         | score, 4 axes, 2-sentence prose, flag-gated logistics                               | **skills required / matched / missing, YOE bar** |
| job_analysis   | full JD + optimized doc, click-gated | structured scorecard incl. skills_matched/missing                                   | little — but 146 rows all-time                   |

**Key structural fact:** the skills-insights pipeline already exists end-to-end
(`SkillFrequency`/`MissingSkill` aggregation → insights page → `foldSkills.ts`
display hygiene, #605) but is fed only by the click-gated analyses. Phase-2
reads the same full JD ~80× more often and feeds it nothing.

## The change

Extend `JobFitResult` (`app/services/fit/job_fit.py`) with capped, structured
lists — no prose:

- `skills_required: list[str]` (≤8, canonical short names) — a fact about the
  **job**; persist to `jobs.skills_required jsonb` (additive migration).
- `skills_matched: list[str]`, `skills_missing: list[str]` (≤5 each) — facts
  about the **(job, target)** pair; persist on the `scores` row.
- Optional, decide at build time: `yoe_required: int | null`.
- **Normalize at write time** (lowercase, collapse whitespace, strip evidence
  clauses) — closes the debt `foldSkills.ts` documents ("until the API-side
  aggregation normalizes at write time").

Aggregation: extend the existing skills-cost/insights computes to read the new
columns (union with the analyses-sourced rows they already fold). FE lights up
with no new page work.

## Marginal cost (measured baselines)

- P2 volume: 1,822 calls / $0.72 per 7d (deepseek-class pricing, $0.0004/call).
- Added output: ~50–100 tokens/call → **≈ pennies per week**. Input unchanged
  (the JD is already in the prompt).
- The REAL cost is the gate: any prompt/schema edit trips
  `test_prompt_regression` → spend-bearing evals + golden re-baseline.

## The single-re-baseline rule (why this is one PR, not several)

Batch every planned prompt/schema change into ONE re-baseline:

1. The harvest fields above (P2 prompt + schema).
2. **Fold in the parked P1 `title_prefix` slimming** — previously recommended
   DROPPED because the re-baseline cost dominated its ~$0.05–0.10/day
   post-collapse savings; if a re-baseline happens anyway, including it is
   nearly free. (It's the #47 id-transposition guard — the slimmed variant
   must keep an equivalent cross-check, e.g. first-word echo or checksum;
   prove on the mock corpus per `.claude/rules/llm-surfaces.md`.)
3. Nothing else rides along without being listed here first.

## Risks / guardrails

- **Grade quality must not move**: the harvest fields are additive output; the
  eval re-baseline exists precisely to prove score/axis stability. If evals
  show drift, ship the harvest WITHOUT the slimming (isolate by running the
  eval matrix per change, not only combined).
- Skill-name noise: bound by write-time normalization + capped lists; the
  existing fold layer stays as display-side defense in depth.
- `_tolerate_malformed`-style degradation (tagger precedent): a malformed
  skills list must degrade to `[]`, never fail the grade. Grow the LLM mock's
  edge battery for the new fields (llm-surfaces rule).
- Backfill: NONE. Fields populate as jobs get (re)graded; insights unions with
  analyses-sourced rows meanwhile. No paid backfill run without a separate
  decision.

## Explicitly out of scope

- P1 enrichment of any kind (wrong end of the cost curve).
- Tagger snippet widening (13k jobs/wk pay input for facts P2 gets free on the
  subset users actually see).
- Comp extraction (JSON-LD + structured-salary arc already own it); logistics
  (#86 owns it).
- Model changes (the deepseek fit bake-off verdict stands unchanged).

## Decision needed from the owner

Authorize the one eval re-baseline (spend: same order as a golden run,
~$1–3 judging by the multi-model bake-off's actuals; estimator undercounts
~3×). On go: single PR = prompt + schema + persistence migration + mock-corpus
growth + eval re-baseline, validated per the house bar.
