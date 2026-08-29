# Phase 1 Title Triage — Multi-Model Run

- Reference model: **sonnet-4.6** (production baseline)
- Titles graded: **960** across 6 targets

## Per-model summary

FN is called out first on purpose: a false negative is a posting Phase 1 drops, and a dropped posting is never ingested and never re-triaged, so the loss is unrecoverable. A false positive only costs one Phase-2 grade.

| Model | **FNR** | FNR+cov | FPR | Agreement vs ref | Coverage | Compared | $ total | $/1k titles | Proj. backlog | Avg latency | p95 | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| haiku-4.5 | **8.1%** | 8.1% | 2.9% | 96.5% | 100.0% | 960 | $0.1687 | $0.1757 | $15.60 | 11512ms | 22942ms | 0 |
| deepseek-v3.2 | **5.4%** | 5.4% | 2.1% | 97.5% | 100.0% | 960 | $0.0488 | $0.0508 | $4.51 | 220363ms | 458110ms | 0 |
| gemini-2.5-flash | **18.0%** | 18.0% | 0.2% | 97.7% | 100.0% | 960 | $0.1148 | $0.1196 | $10.62 | 13142ms | 26001ms | 0 |
| gemini-flash-lite | **18.0%** | 18.0% | 1.1% | 97.0% | 100.0% | 960 | $0.0156 | $0.0163 | $1.44 | 7224ms | 14616ms | 0 |
| llama-3.3-70b | **7.2%** | 7.2% | 2.8% | 96.7% | 100.0% | 960 | $0.0111 | $0.0116 | $1.03 | 143621ms | 368710ms | 0 |
| qwen3-235b | **11.7%** | 11.7% | 0.8% | 97.9% | 100.0% | 960 | $0.0212 | $0.0221 | $1.96 | 56142ms | 176682ms | 0 |
| mistral-small-3.2 | **3.6%** | 4.5% | 1.0% | 98.7% | 96.9% | 930 | $0.0073 | $0.0076 | $0.68 | 51600ms | 184207ms | 2 |
| sonnet-4.6 (ref) | — | — | — | — | — | — | $0.5048 | $0.5258 | $46.69 | 18614ms | 37967ms | 0 |

> Projected backlog = 14,800 un-triaged postings × 6 active targets = 88,800 (target, title) pairs. Phase 1 grades every free-gate survivor against every unblocked target, so the target count is a multiplier on the bill.

## Per-stratum breakdown

`own_gate` = this target's own free gate admits the title (the hard, ambiguous pairs). `cross_gate` = the title only survived because a *different* target's gate admitted it (mostly easy off-family rejects). A headline agreement carried by `cross_gate` says little.

| Model | cross_gate agree / FNR / cov (n) | own_gate agree / FNR / cov (n) |
| --- | --- | --- |
| haiku-4.5 | 97.5% / 33.3% / 100.0% (732) | 93.0% / 1.1% / 100.0% (228) |
| deepseek-v3.2 | 97.7% / 16.7% / 100.0% (732) | 96.9% / 2.3% / 100.0% (228) |
| gemini-2.5-flash | 97.5% / 66.7% / 100.0% (732) | 98.2% / 4.6% / 100.0% (228) |
| gemini-flash-lite | 97.0% / 66.7% / 100.0% (732) | 96.9% / 4.6% / 100.0% (228) |
| llama-3.3-70b | 96.9% / 29.2% / 100.0% (732) | 96.0% / 1.1% / 100.0% (228) |
| qwen3-235b | 98.1% / 37.5% / 100.0% (732) | 97.4% / 4.6% / 100.0% (228) |
| mistral-small-3.2 | 98.7% / 12.5% / 96.5% (706) | 98.7% / 1.2% / 98.2% (224) |

## Production admission decision (promising AND confidence >= 40)

The table above scores the raw PROMISING verdict. Production admits on `title_triage.admitted` — a promising call the model hedges below the confidence floor is DROPPED. Same responses, re-scored under that rule.

| Model | **FNR** | FPR | Agreement vs ref | Coverage | Compared |
| --- | --- | --- | --- | --- | --- |
| haiku-4.5 | **8.1%** | 2.9% | 96.5% | 100.0% | 960 |
| deepseek-v3.2 | **5.4%** | 2.1% | 97.5% | 100.0% | 960 |
| gemini-2.5-flash | **18.0%** | 0.2% | 97.7% | 100.0% | 960 |
| gemini-flash-lite | **18.0%** | 1.1% | 97.0% | 100.0% | 960 |
| llama-3.3-70b | **7.2%** | 2.8% | 96.7% | 100.0% | 960 |
| qwen3-235b | **11.7%** | 0.8% | 97.9% | 100.0% | 960 |
| mistral-small-3.2 | **3.6%** | 1.0% | 98.7% | 96.9% | 930 |

## Per-target agreement

| Target | haiku-4.5 | deepseek-v3.2 | gemini-2.5-flash | gemini-flash-lite | llama-3.3-70b | qwen3-235b | mistral-small-3.2 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Product Manager | 93.8% | 98.8% | 100.0% | 99.4% | 95.0% | 97.5% | 99.4% |
| Software Engineer | 92.5% | 98.8% | 98.8% | 96.9% | 96.2% | 98.8% | 98.1% |
| Product Designer | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| Data Scientist / ML Engineer | 100.0% | 93.8% | 98.8% | 98.1% | 98.1% | 99.4% | 99.3% |
| DevOps / SRE Engineer | 98.8% | 96.9% | 98.1% | 98.1% | 98.1% | 98.1% | 98.7% |
| Senior Frontend Engineer | 93.8% | 96.9% | 90.6% | 89.4% | 92.5% | 93.8% | 96.9% |
