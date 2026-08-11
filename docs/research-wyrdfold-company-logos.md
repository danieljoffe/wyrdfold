# Research: company logos on search/jobs surfaces (#470)

Exploration record, 2026-07-30. Two parts: the repo-side findings (what exists
in our schema, from the issue's original exploration) and the external
landscape (services + datasets, researched when the issue was picked up).
Constraint (owner): **store only LINKS to logos, never copies of the images.**

## Problem shape

Search results and job cards render a deterministic initials monogram per
company. Real logos would speed up skimming. The blocker is not "store a logo
URL" — logo services key on a company **domain**, and we don't store one and
can't reliably derive it. The real work is **domain enrichment**.

## Repo-side findings (verified against schema + live DB)

- **No company entity exists.** No `companies` table or view; a "company" is a
  free-text string: `jobs.company_name` (copied onto every row) and
  `sources.company_name` (one per ATS board, no unique constraint). The only
  stable company-ish key is `sources.board_token` (UNIQUE — the ATS slug).
- **No domain/website/logo is stored anywhere.** `jobs.absolute_url` is the
  ATS board link, not the company site. The slug is a **lossy** domain stem
  (`linear56`, `dbtlabs`, `retool35` → `{slug}.com` is wrong for a meaningful
  fraction). ATS payloads don't carry a domain.
- **The natural enrichment point is source creation** (`ats_detect.py` /
  `source_registration.py` / `register_source_from_url`) + a one-time backfill
  — dozens–hundreds of rows, not per-job thousands. The poller copies
  `sources.company_name` onto jobs, so `sources` is the right home for
  `domain` (+ optional `logo_url`).

## External landscape (2026-07)

### Logo-link services — solved; build and maintain nothing

| Service                                                            | Terms                                                                                                                                            | Verdict                                                                |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| ~~Clearbit Logo API~~                                              | **Dead** — sunset 2025-12-08 post-HubSpot-acquisition                                                                                            | Any Clearbit URL in older notes is stale                               |
| **Brandfetch Logo Link** `cdn.brandfetch.io/{domain}?c={clientId}` | 500k req/mo free (soft), **no attribution**, free clientId, hotlink **required**, 2,400 req/5min                                                 | **Primary** — their hotlink requirement _is_ our links-only constraint |
| logo.dev `img.logo.dev/{domain}?token=…`                           | 500k/mo free, official Clearbit migration path, but **commercial free-tier use requires a visible attribution link** on every page showing logos | Fallback-grade; worse terms for us                                     |
| Google s2 favicons / DuckDuckGo icons                              | Token-free favicon endpoints; unofficial, no SLA/ToS blessing                                                                                    | Second tier in the client `onError` cascade                            |
| unavatar (microlinkhq, OSS)                                        | Aggregator cascading DDG→Google→Microlink; hosted free tier now ~50/day; self-hosting = running a service                                        | Skip — a client-side `onError` cascade does the same for free          |

**Recommended render chain (all client-built links, zero storage):**
Brandfetch → DuckDuckGo/Google favicon → existing initials monogram on error.

### Name/slug → domain — no reusable open collection exists

- companydatacom/public-datasets (CC0): firmographics only, **no domains**.
- Feashliaa/job-board-aggregator: ~95k ATS company identifiers harvested from
  Common Crawl — the closest thing to an open ATS-slug registry, but
  **CC BY-NC** (unusable commercially without permission) and it maps slugs to
  nothing (no domains, no logos). Its Common-Crawl harvesting technique is a
  useful precedent, not a dependency.
- Wikidata **P856** (official website): CC0 + SPARQL — good one-shot booster
  for well-known companies; spotty for the startups that dominate ATS boards.
- Older bulk dumps (e.g. the 2019 PDL "7M companies" release) are stale and
  license-murky for embedding in a commercial product.
- Verify at build time: **Brandfetch Brand Search API** (name→domain, same
  free registration) could seed the enrichment, with resolve-verify as the
  gate.

## Updated build plan (when picked up)

1. **Migration:** `sources.domain text` nullable (skip `logo_url` — the client
   builds provider URLs from the domain, so switching providers is a
   frontend-only change).
2. **Enrichment (once per source):** candidate domain from slug/name
   (+ Brandfetch search / Wikidata as boosters) → **verify it resolves**
   (reuse `services/safe_http.py`) → store. One-time backfill over existing
   sources (~3.2k).
3. **Read paths:** `/search` service adds a `jobs → sources` join for
   `domain`; the list RPCs (`get_target_jobs` / `get_cross_target_jobs`) add
   the same pass-through if logos should reach the matched surfaces.
4. **FE:** build the Brandfetch URL from `domain`, cascade `onError` to a
   favicon service, fall back to the initials avatar. Register the free
   Brandfetch clientId (env var, both Vercel + local).

## Open-source consideration (owner-requested)

There is a genuine void: nobody maintains an open ATS-slug→domain mapping —
the one adjacent dataset is non-commercial and domain-less. Once enrichment
runs, our `sources` table (provider, board_token, company_name, verified
domain) _is_ that dataset. Publishing it as `ats-company-domains` (CC0 export

- Actions-refreshed, PR-correctable) would fill the void and invite community
  corrections back into our own data quality, at the cost of one export script
  and a review loop. **Decision deferred until the build proves mapping accuracy
  on our sources; publishing is a cheap follow-on since the data accrues either
  way.**

## Sources

- HubSpot sunset notice: developers.hubspot.com/changelog/upcoming-sunset-of-clearbits-free-logo-api
- Clearbit help (sunset confirmation): help.clearbit.com
- Brandfetch Logo API docs: docs.brandfetch.com/logo-api/overview
- logo.dev attribution + pricing: logo.dev/docs/platform/attribution · logo.dev/pricing
- unavatar: github.com/microlinkhq/unavatar
- companydatacom/public-datasets: github.com/companydatacom/public-datasets
- Feashliaa/job-board-aggregator: github.com/Feashliaa/job-board-aggregator
