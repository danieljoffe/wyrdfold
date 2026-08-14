# CLAUDE.md — wyrdfold

Repo-specific facts and pointers. The machine-wide engineering rules
([danieljoffe/claude-rules](https://github.com/danieljoffe/claude-rules), loaded via
`~/.claude/CLAUDE.md`) cover the working rhythm, durable-over-quick, prove-the-diagnosis,
validate-before-PR, review-before-merging, and the `develop`/`main` + `gh` basics. This
file wins on conflict.

## What this is

AI job-matching SaaS: poll ATS boards (Greenhouse/Ashby/Lever/…) → score against user
targets (deterministic keywords + LLM phases) → matched `/jobs`, public `/search`,
résumé tailoring. Live at wyrdfold.com (Vercel FE + Railway API + hosted Supabase).

## Layout & stack

- `apps/wyrdfold` — Next.js 16 App Router FE (pnpm + nx). UI primitives come from
  `@danieljoffe/shared-ui` — **read
  `node_modules/@danieljoffe/shared-ui/README.md` before building any form
  control/menu**; don't hand-roll what it ships.
  - **Next 16 is NOT the Next.js in model training data** — APIs, conventions, and
    file structure changed. Before writing Next-specific code (routing, caching,
    config, middleware), read the relevant shipped guide in
    `apps/wyrdfold/node_modules/next/dist/docs/` and heed deprecation notices.
    (This folds in Next's auto-generated agent rules; generation is disabled via
    `agentRules: false` in `next.config.mjs`.)
- `apps/wyrdfold-api` — FastAPI (Python 3.11, uv workspace). Poller, scoring, LLM
  pipeline (`app/services/`), routers.
- `apps/wyrdfold-e2e` — Playwright. `supabase/migrations/` — Postgres schema (manual
  apply; see rules).

## Key commands

- API tests: `cd apps/wyrdfold-api && uv run pytest -q` (+ `ruff check`, `mypy app`).
  Integration tests need `pytestmark = pytest.mark.integration` (run `-m integration`).
- FE: `nx test wyrdfold` (jest — run before any FE PR; `nx build` only typechecks),
  `nx lint wyrdfold`. Local FE dev on port 3200; local Supabase on 54321/54322.

## Deploy topology (the part that bites)

Railway auto-deploys the **API** from `main`. **Vercel is NOT git-connected** — every
release runs `vercel --prod`. **Migrations are manual** — apply + verify around the
merge. Full gate: the **`/release` skill** (`.claude/skills/release/`). History of why:
`docs/decisions.md`.

## Path-scoped rules

`.claude/rules/api-validation.md` (Docker-image validation, migration discipline) and
`.claude/rules/llm-surfaces.md` (grow the LLM mock's bug corpus) load when matching
files are touched. See also `CONTRIBUTING.md` → "Before opening a PR".

## Repo governance specifics

- `.github/workflows/pr-base-branch.yml` fails a PR into `main` from anything but
  `develop` / `release/*` / `hotfix/*` (so a hotfix may target `main` directly).
