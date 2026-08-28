# Phase-1 triage model bake-off — the unadmitted stack

**Date** 2026-08-28 · **Question** which cheap model can do global Phase-1 title
triage most cheaply without losing accuracy, before Phase 1 is enabled for the
shared catalog targets · **Oracle** `sonnet-4.6` · **Incumbent** `deepseek-v3-2`
(`PHASE1_TRIAGE_MODEL`, confirmed live in Railway) · **Measured run cost**
$0.892 over 96 calls, against a $0.91 pre-run estimate.

Reproduce:

```bash
cd apps/wyrdfold-api
railway run -- uv run python -m scripts.build_phase1_unadmitted_corpus \
    --output tests/fixtures/phase1_unadmitted_corpus.json
railway run -- uv run python scripts/eval_phase1_triage.py \
    --fixture tests/fixtures/phase1_unadmitted_corpus.json \
    --reference sonnet-4.6 --batch-size 150 --temperature 0 --min-confidence 40
```

---

## The short version

**Do not swap on this evidence.** On the surface the question is actually about
— the five shared catalog targets — every one of the seven candidates lands
between 0 and 5 false negatives out of 87 oracle-positives, and every confidence
interval overlaps every other. Quality is a tie. The corpus separates the
candidates only on a target that is *not* a catalog target, and on that target
the oracle's own calls look wrong (below).

What the run *does* establish is a cost and latency case against the incumbent
that has nothing to do with accuracy:

- **deepseek-v3.2 is 6.7× the cost per title of the cheapest candidate** and the
  **slowest of all eight models measured** — 220 s average per 150-title batch
  against mistral's 52 s and gemini-flash-lite's 7 s.
- **Its billed rate is ~2.9× what our own cost table assumes.** `pricing.py`
  prices `deepseek-v3-2` at $0.27/M in, $0.40/M out; the same token counts
  OpenRouter actually billed work out to roughly $0.83/M in, $1.10/M out.
  Internal Phase-1 spend records are therefore understated. This is true today,
  independent of any model swap.

The strongest challenger is **mistral-small-3.2**, and the one thing that
separated it from the field separated it in the wrong direction: it was the only
model to lose coverage.

---

## Corpus — the unadmitted stack, not the catalog

`jobs` is the wrong corpus for this question. Every row in it already cleared
admission, so it is a sample of already-promising postings; it flatters every
model and cannot measure the error that matters. The postings Phase 1 actually
judges are the ones it drops, and those were never persisted.

`scripts/build_phase1_unadmitted_corpus.py` rebuilds that stack from live boards
and the poller's own gate functions (imported from `poller`, not restated):

| stage | count |
| --- | ---: |
| sources sampled (of 4,832 enabled) | 155 |
| sources fetched successfully | 153 (2 board 404/errors) |
| postings listed | 5,581 |
| already held in `jobs` | 637 |
| new to us | 4,944 |
| rejected by the free gates | 4,420 |
| **free-gate survivors (the unadmitted stack)** | **524** |

524 survivors from 153 source-polls is **3.4 per source-poll**, which
independently reproduces the ~3/source figure the backlog estimate rests on.
Extrapolated across 4,832 enabled sources that is roughly **16,400 un-triaged
postings**, in the same range as the ~14,800 working figure.

Deduped to 160 distinct titles and crossed with all 6 pipeline-active targets →
**960 (target, title) pairs**. The cross product is the right unit:
`_poll_one_source` builds one `triage_candidates` list per source and grades it
against *every* unblocked active target, which is exactly why enabling Phase 1
for the catalog makes triage a global cost.

Committed as `tests/fixtures/phase1_unadmitted_corpus.json`.

### What the committed fixture contains

This repo is public and the corpus is built from production targets, so the
fixture stores the **minimum that reproduces the eval** — per target, exactly
five fields:

| field | why it is kept |
| --- | --- |
| `label` | read by the Phase-1 prompt (`_split_user_message`) |
| `example_promising_titles` | read by the Phase-1 prompt |
| `example_unpromising_titles` | read by the Phase-1 prompt |
| `app_active` | bare boolean, no user-derived content; required to construct a `JobTarget`, and carries the catalog-vs-followed split the whole analysis turns on |
| `id` | a **fixture-local alias** (`catalog-software-engineer`, `user-senior-frontend-engineer`), never a production row id |

Everything else is dropped, including **`scoring_profile` and
`search_keywords`**. Those drive the free gate at *build* time
(`_passes_free_gates`, and the `stratum` tag), but both outcomes are baked into
`cases` as data, so the committed artifact has no use for the configuration that
produced them — and a user-followed target's profile is LLM-derived from that
user's own résumé. Also dropped: `description` (#868), `normalized_label`,
`role_family`, `seniority_hint`, `activation_status`, `profile_version`,
`created_at`, `updated_at`.

The allowlist is enforced in CI (`PERMITTED_TARGET_KEYS`), as a *subset*
assertion rather than an absence check — a denylist catches the field you
already found, not the next one.

### Rerunnable, not reproducible

The builder samples **live** target configuration and **live** boards at build
time. Boards move, and targets and their example pools are edited in the
database independently of this repo — so rebuilding at the same `--seed` does
**not** reproduce this corpus or this decision boundary. It reruns the same
procedure against the then-current production configuration.

Treat the committed fixture as the immutable artifact: the bake-off results
below are evidence for *this* file, not for whatever a rebuild would produce.
`meta.target_config_digests` records a one-way fingerprint of each target's
configuration as used here, so a later rebuild can tell whether the boundary
moved without the fixture carrying the configuration itself.

### Corpus skew — read this before trusting the ranking

- **Provider mix is balanced by construction, not fleet-weighted.** 32 titles
  from each of greenhouse / ashby / lever / workday / smartrecruiters. The real
  fleet is 35% greenhouse and 0.4% smartrecruiters, so smartrecruiters is
  massively over-represented and greenhouse under-represented. This was
  deliberate (one board's naming conventions must not become the corpus) but it
  means the title distribution is not the production distribution.
- **The FN denominator is small.** The oracle marked only **111 of 960 pairs
  promising (11.6%)**, so every false-negative rate is measured over ~111 events
  and one extra miss moves the rate by ~0.9 points.
- **Oracle-positives are wildly uneven across targets**: Software Engineer 46,
  Senior Frontend Engineer 24, Data Scientist/ML 16, Product Manager 11, DevOps
  10, **Product Designer 4**. Product Designer contributes almost nothing —
  every model scores 100% agreement there because there is nothing to disagree
  about.
- **One 160-title sample per target, one run per model.** No repeats, so
  run-to-run variance is unmeasured.

---

## Headline results (all 960 pairs)

Agreement with `sonnet-4.6`, batch size 150 (deepseek's production cap),
temperature 0 (matching production's `complete_json`).

| Model | FN | **FNR** | FP | FPR | Agreement | Coverage | $/1k titles | rel. cost | avg latency | p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| mistral-small-3.2 | 4 | **3.6%** | 8 | 1.0% | 98.7% | 96.9% ⚠️ | $0.0076 | 1.0× | 51.6 s | 184 s |
| deepseek-v3.2 *(incumbent)* | 6 | **5.4%** | 18 | 2.1% | 97.5% | 100% | $0.0508 | 6.7× | **220.4 s** | **458 s** |
| llama-3.3-70b | 8 | **7.2%** | 24 | 2.8% | 96.7% | 100% | $0.0116 | 1.5× | 143.6 s | 369 s |
| haiku-4.5 | 9 | **8.1%** | 25 | 2.9% | 96.5% | 100% | $0.1757 | 23.0× | 11.5 s | 23 s |
| qwen3-235b | 13 | **11.7%** | 7 | 0.8% | 97.9% | 100% | $0.0221 | 2.9× | 56.1 s | 177 s |
| gemini-2.5-flash | 20 | **18.0%** | 2 | 0.2% | 97.7% | 100% | $0.1196 | 15.7× | 13.1 s | 26 s |
| gemini-flash-lite | 20 | **18.0%** | 9 | 1.1% | 97.0% | 100% | $0.0163 | 2.1× | **7.2 s** | 15 s |
| *sonnet-4.6 (oracle)* | — | — | — | — | — | — | $0.5258 | 69.0× | 18.6 s | 38 s |

Read the FN column, not the agreement column. Agreement is dominated by the 849
pairs the oracle called UNPROMISING; every model gets ~97-99% of those right, so
the agreement spread (96.5-98.7%) is nearly meaningless. **gemini-2.5-flash has
the second-highest agreement in the table and the joint-worst FN rate** — that
is the exact trap the headline number sets.

Backlog projection at these rates: 14,800 postings × 6 targets = 88,800 pairs →
$0.68 (mistral) to $15.60 (haiku), incumbent $4.51. See the caveat below on why
these are best-case for every model.

---

## The result that decides the question

Split by whether the target is a **shared catalog target** (`app_active`, the
five the owner is about to enable) or the one **user-followed** target:

### Catalog targets only — 5 targets, 800 pairs, 87 oracle-positives

| Model | FN | FNR | 95% CI (Wilson) | FP | FPR | Agreement | Coverage |
| --- | ---: | ---: | :---: | ---: | ---: | ---: | ---: |
| haiku-4.5 | 0/87 | 0.0% | [0.0%, 4.2%] | 24 | 3.4% | 97.0% | 100% |
| deepseek-v3.2 | 1/87 | 1.1% | [0.2%, 6.2%] | 18 | 2.5% | 97.6% | 100% |
| llama-3.3-70b | 1/87 | 1.1% | [0.2%, 6.2%] | 19 | 2.7% | 97.5% | 100% |
| mistral-small-3.2 | 2/86 | 2.3% | [0.6%, 8.1%] | 5 | 0.7% | 99.1% | 96.2% ⚠️ |
| gemini-flash-lite | 3/87 | 3.4% | [1.2%, 9.7%] | 9 | 1.3% | 98.5% | 100% |
| qwen3-235b | 3/87 | 3.4% | [1.2%, 9.7%] | 7 | 1.0% | 98.8% | 100% |
| gemini-2.5-flash | 5/87 | 5.7% | [2.5%, 12.8%] | 2 | 0.3% | 99.1% | 100% |

Zero to five misses. **Every interval overlaps every other interval.** On the
decision surface, this corpus cannot tell these seven models apart on quality.

### The user-followed target only — Senior Frontend Engineer, 160 pairs, 24 oracle-positives

| Model | FN | FNR | 95% CI |
| --- | ---: | ---: | :---: |
| mistral-small-3.2 | 2/24 | 8.3% | [2.3%, 25.8%] |
| deepseek-v3.2 | 5/24 | 20.8% | [9.2%, 40.5%] |
| llama-3.3-70b | 7/24 | 29.2% | [14.9%, 49.2%] |
| haiku-4.5 | 9/24 | 37.5% | [21.2%, 57.3%] |
| qwen3-235b | 10/24 | 41.7% | [24.5%, 61.2%] |
| gemini-2.5-flash | 15/24 | 62.5% | [42.7%, 78.8%] |
| gemini-flash-lite | 17/24 | 70.8% | [50.8%, 85.1%] |

**Every model's headline FN rate is driven by this one target** — 9 of haiku's 9
misses, 5 of deepseek's 6, 17 of flash-lite's 20. And it is the one target the
owner's question does *not* cover.

### …and on that target the oracle is probably the one that is wrong

Eleven of the 24 titles `sonnet-4.6` called PROMISING for a **Senior Frontend
Engineer** target were rejected by three or more of the seven candidates:

```
7/7 rejected: "Machine Learning Engineer"
6/7 rejected: "Member of Technical Staff (Software Engineer, Desktop Apps)"
6/7 rejected: "Senior Software Engineer (Cryptography)"
5/7 rejected: "Lead Software Engineer (Team Lead)"
5/7 rejected: "Senior Software Engineer II (Auth)"
5/7 rejected: "Member of Technical Staff (Software Engineer, Computer Growth)"
5/7 rejected: "Member of Technical Staff (Software Engineer, Acceleration)"
5/7 rejected: "Software Engineer, Model Evaluation and Improvement"
4/7 rejected: "Senior Software Engineer II"
4/7 rejected: "Software Engineering Lead"
3/7 rejected: "Junior Engineer"
```

The prompt says adjacent specializations are PROMISING when they *share the
target's core discipline*, and that a Senior Backend Engineer for a Frontend
target is UNPROMISING. "Machine Learning Engineer" for a Senior Frontend
Engineer target is a different specialization by that rule, and seven
independent models agreeing against the oracle is not what seven independent
errors look like. The most likely reading is that **sonnet leans promising much
harder than the prompt asks on narrow specialist targets**, and the cheap models
are being scored as wrong for being right.

Which means: the one place this corpus produced a quality signal is also the one
place the metric is least trustworthy. That is not a basis for a swap, in either
direction.

There is a real behavioural pattern underneath, though, and it is worth keeping:
**the cheap models are stricter than the oracle on narrow targets and agree with
it on broad ones.** All five catalog targets are broad ("Software Engineer",
"Product Manager"). If the catalog later adds a narrow specialist target, the
model choice starts to matter in a way it does not today — and gemini-flash-lite
is the one that would hurt most.

---

## Cost, measured

Per-token rates recovered by least squares from each model's 12 calls. The
method validates itself: it reproduces four published list prices exactly
(haiku 1.00/5.00, sonnet 3.00/15.00, gemini-flash 0.30/2.50, flash-lite
0.10/0.40).

| Model | $/M input | $/M output | $/1k titles (measured) |
| --- | ---: | ---: | ---: |
| mistral-small-3.2 | 0.075 | 0.200 | $0.0076 |
| gemini-flash-lite | 0.100 | 0.400 | $0.0163 |
| llama-3.3-70b | 0.129 | 0.303 | $0.0116 |
| qwen3-235b | 0.153 | 0.612 | $0.0221 |
| gemini-2.5-flash | 0.300 | 2.500 | $0.1196 |
| **deepseek-v3.2** | **0.826** | **1.102** | **$0.0508** |
| haiku-4.5 | 1.000 | 5.000 | $0.1757 |

### Finding: our cost table understates deepseek

`app/services/llm/pricing.py` prices `deepseek-v3-2` at **$0.27/M in, $0.40/M
out**. Applied to the tokens this run actually consumed (22,240 in / 27,558 out)
that predicts $0.0170; OpenRouter billed **$0.0488** — **2.9×**. So every
`llm_costs` row for `relevance.title_triage` understates real Phase-1 spend by
roughly that factor, and so does anything built on it (budget gates, allowance
accounting).

The cause is visible in the routing: `deepseek/deepseek-v3.2` is served by 14
OpenRouter providers spanning **$0.209/M in + $0.31/M out** (GMICloud) to
**$3.00 + $4.50** (SambaNova). The slug is not pinned to a provider, so the
price we pay is a routing outcome, not a property of the model. All six of this
run's large batches landed on the same expensive end (~$1.47/M implied output,
9-12 tok/s). `pricing.py` also prices haiku at 0.80/4.00 where the measured list
rate is 1.00/5.00 — a smaller 25% understatement, same class of drift.

Prod mapping confirmed identical to the eval's: `openrouter_client.py` maps
`deepseek-v3-2` → `deepseek/deepseek-v3.2`.

### Caveat: these $/1k figures are best-case for every model

The eval used 150-title batches, which amortize the ~1,600-token system +
target-context prefix over 150 titles. **Production does not look like that.**
400 recent prod `relevance.title_triage` rows:

| | prod (median) | this eval (per 150-batch) |
| --- | ---: | ---: |
| output tokens/call | 79 (≈2-3 verdicts) | ~4,400 |
| input tokens/call | 154 + 1,600 cache-read | ~2,000 |
| input : output ratio | **5.7 : 1** | 0.81 : 1 |

Prod Phase-1 is **input-dominated**; the eval was output-dominated. Prompt
caching is working (1,600-token cached prefix on 265 of 400 calls), which
absorbs most of it, but the practical consequence stands: in production a
model's *input* price matters more than this eval's batch shape implies.
Fortunately that does not disturb the top of the ranking — mistral is cheapest
on both axes, and deepseek is 11× mistral on input and 5.5× on output.

## Latency, measured

Phase 1 runs inline in the poll cycle, so this is not a footnote.

Average / p95 per 150-title batch: flash-lite **7.2 / 15 s** · haiku 11.5 / 23 s
· gemini-flash 13.1 / 26 s · sonnet 18.6 / 38 s · mistral 51.6 / 184 s · qwen
56.1 / 177 s · llama 143.6 / 369 s · **deepseek 220.4 / 458 s**.

Measured under 8-way concurrency across mixed providers, so treat these as
indicative rather than a clean benchmark — **except for deepseek, which
production corroborates**: 400 real prod Phase-1 calls run at a median **17.9
output tok/s** with a max latency of **147 s**. The eval saw 9-12 tok/s. Same
slug, same order of magnitude, and it is throughput-bound, not queueing.

## Reliability — the one thing that separated the leader, in the wrong direction

mistral-small-3.2 was the **only** model below 100% coverage: 930 of 960 pairs
(96.9% overall, 96.2% on catalog targets). It clears the harness's 95% hard
floor, but the cause matters. Three of its twelve calls returned no verdicts,
all on the ragged 10-title tail chunks:

- 2 × `HTTP 429 Provider returned error` (rate limit)
- 1 × HTTP 200 with an **empty body** and zero-token usage

In production semantics these are two different failures. A 429 raises
`LLMServiceError` → `triage_titles` returns `({}, None)` → the poller leaves
those titles un-attempted → they **defer** and re-triage next cycle. No posting
is lost; a cycle of latency is. The empty 200 is worse-behaved: it would parse
as a *successful* call with no verdicts, so every title in it **fails open and
is admitted untriaged** — that costs Phase-2 grades rather than postings, but it
is silent.

Every other model: 12/12 clean calls, 100% coverage, zero errors.

## Two things that turned out to be inert

- **The confidence floor does nothing on this corpus.** Replaying every response
  through the real `title_triage.admitted` at `PHASE1_MIN_CONFIDENCE=40` changed
  **not one verdict** for any model — the lowest confidence attached to any
  PROMISING call anywhere in the run was exactly 40 (llama; sonnet's minimum was
  42, everyone else ≥45). The admission-gated table is identical to the raw one.
  The floor is not currently filtering anything.
- **The `own_gate` / `cross_gate` split did not expose a cheat.** Both strata
  behave consistently per model; nobody is carrying a headline on the easy half.
  (The `cross_gate` FN *rates* look alarming — 12.5% to 66.7% — but the
  denominator there is 24 oracle-positives spread over 732 pairs, so they are
  3-16 events. Not a signal.)

---

## Recommendation

**1. Do not swap Phase-1 models on this run.** The incumbent won a prior
bake-off on quality-per-dollar and the burden is on a challenger to beat it on
*both* axes. mistral-small-3.2 beats it decisively on cost (6.7×) and latency
(4.3×), **ties** on quality (2/87 vs 1/87 catalog FN; paired exact test on the
oracle-promising pairs both models answered, p = 0.688), and **regresses** on
reliability (the only coverage miss,
including a silent empty-200). That is one axis won, one tied, one lost — not
the bar.

**2. Do fix the cost table now, independent of any swap.** `pricing.py`
`deepseek-v3-2` at 0.27/0.40 understates the real bill ~2.9×, and haiku at
0.80/4.00 understates ~1.25×. Everything downstream of `llm_costs` — budget
gates, per-payer allowance, the daily cap — is reading low. This is a live
correctness bug in spend accounting, not an eval artifact.

**3. Then run the confirmation the swap actually needs**, which this run cannot
substitute for:

- **A corpus with a real positive count.** 87 catalog oracle-positives cannot
  separate 0 misses from 5. Enrich for oracle-positives (pre-screen survivors
  with a cheap pass and oversample the promising side) to get several hundred,
  and report FN over that.
- **Ground-truth labels on a hard subset, not just oracle agreement.** The one
  place this corpus produced a signal is the one place the oracle looks wrong.
  A hand-labelled set of ~100 boundary titles would settle whether sonnet's
  leniency on narrow targets is correct behaviour or oracle error — and it would
  make every future run measure correctness instead of drift.
- **A reliability soak for mistral.** Several hundred sequential calls at the
  production batch shape, counting 429s and empty-200s. If the 429 rate is a
  blip it is a non-issue (429s defer safely); if it is a pattern, mistral is
  disqualified regardless of price.
- **Pin the provider, then re-measure.** Both the price and the speed of
  `deepseek/deepseek-v3.2` in this run were routing outcomes across a 14-provider
  spread. Comparing an unpinned incumbent against an unpinned challenger
  compares routing luck as much as models. Pinning deepseek to a cheap fast
  endpoint might resolve the entire cost-and-latency case against it without
  changing models at all — that possibility has to be eliminated before a swap
  can be justified on cost.

**4. Ranking, if a swap is forced today** (catalog targets, cost-and-latency
ordered, quality being a tie): mistral-small-3.2 → llama-3.3-70b →
gemini-flash-lite → qwen3-235b → **deepseek-v3.2 (incumbent)** →
gemini-2.5-flash → haiku-4.5. Note that llama fixes the cost problem but not the
latency one (144 s/batch), and that gemini-flash-lite — fastest, 3.1× cheaper
than the incumbent, 100% coverage — is the tempting choice to avoid: 70.8% FN on
the one narrow target in the set. **Rule out gemini-2.5-flash outright**: worst
FN of the cheap tier at 15.7× the cheapest model's cost.

---

## What this run could NOT determine

1. **Whether any candidate is genuinely more accurate than the incumbent.** On
   catalog targets all seven sit within one overlapping band (0-5 FN of 87).
   Paired exact tests over all 960 pairs: mistral vs deepseek p = 0.688,
   mistral vs haiku p = 0.180, deepseek vs haiku p = 0.453. Nothing separable.
2. **Whether the oracle is right.** This is agreement, not correctness. There
   are no ground-truth labels, sonnet's own FN rate is unmeasured, and the
   evidence above suggests it is over-lenient on narrow targets — which would
   mean the headline FN rates are inflated for every model. Paired exact tests
   used here: mistral vs deepseek 2 vs 4 discordant misses (p = 0.688), mistral
   vs haiku 2 vs 7 (p = 0.180), deepseek vs haiku 2 vs 5 (p = 0.453), llama vs
   deepseek 5 vs 3 (p = 0.727).
3. **Whether the latency numbers generalize.** One run, 8-way concurrency,
   unpinned routing. Only deepseek's figure is corroborated by production.
4. **Absolute production cost.** The eval's 150-title batches are best-case
   amortization; prod's Phase-1 calls are ~3 titles and input-dominated (5.7:1).
   Ratios between models are more trustworthy than the absolute $/1k.
5. **Provider reliability over time.** 96 calls total. mistral's two 429s could
   be a blip or a pattern; nothing here distinguishes them.
6. **Run-to-run variance.** One sample per (model, batch), temperature 0 but no
   repeats, so none of these numbers carry a stability estimate.
7. **Behaviour on narrow catalog targets.** All five current catalog targets are
   broad. The only narrow target in the corpus is user-followed, and it is the
   one where the oracle is least trustworthy — so the "what if the catalog adds
   a specialist target" question is genuinely open.
8. **Whether Phase 2 absorbs the false positives.** FP rates here (0.2-3.4%)
   are only cheap if Phase 2 actually filters them; that downstream cost was not
   measured.

Raw run output (`phase1_bakeoff_unadmitted_20260828.json`, ~400 KB of per-verdict
detail) is deliberately not committed — `eval_results/` is gitignored for raw
output. Every confusion matrix needed to recheck the arithmetic above is in the
tables. The corpus fixture and the harness are committed, so the **grading** step
can be re-run against this exact corpus for ~$0.90 (model sampling still varies
run to run); the **corpus build** cannot be replayed — see "Rerunnable, not
reproducible" above.
