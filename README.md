# WyrdFold

Career-decision and job-search tooling. WyrdFold polls job sources against your
targets, scores postings for fit, and helps tailor resumes and cover letters.

This is a standalone **Nx + pnpm** monorepo (polyglot: TypeScript + Python),
extracted from the `danieljoffe.com` monorepo. The shared design system is
consumed as the published npm package [`@danieljoffe/shared-ui`](https://www.npmjs.com/package/@danieljoffe/shared-ui).

## Architecture

| Project                 | Stack                                                         | Deploy           |
| ----------------------- | ------------------------------------------------------------- | ---------------- |
| **`apps/wyrdfold`**     | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4 | Vercel           |
| **`apps/wyrdfold-api`** | FastAPI (Python 3.11+), `uv` workspace                        | Railway (Docker) |
| **`apps/wyrdfold-e2e`** | Playwright E2E for the web app                                | —                |

- **Database & auth:** Supabase (Postgres + `pgvector`). Auth is **magic-link only**. CLI config lives in [`supabase/`](./supabase).
- **Web → API:** the Next.js app proxies to `wyrdfold-api` (`WYRDFOLD_API_URL`), forwarding the user's Supabase JWT as a Bearer token. The API verifies it against Supabase's JWKS endpoint (derived from `SUPABASE_URL`).

## Prerequisites

- **Node.js 24.x** and **pnpm** (`packageManager` is pinned)
- **[uv](https://docs.astral.sh/uv/)** for the Python API
- **[Supabase CLI](https://supabase.com/docs/guides/cli)** for DB work

## Getting started

```bash
pnpm install            # installs JS deps; postinstall runs `uv sync` for Python

# Env: copy the templates and fill in values
cp apps/wyrdfold/.env.example apps/wyrdfold/.env.local
cp apps/wyrdfold-api/.env.example apps/wyrdfold-api/.env

pnpm nx dev wyrdfold        # web app  → http://localhost:3100
pnpm nx dev wyrdfold-api    # FastAPI  → http://localhost:8001
```

## Common commands

```bash
pnpm nx build wyrdfold                 # production build of the web app
pnpm nx test wyrdfold                  # web unit tests (Jest + RTL)
pnpm nx e2e wyrdfold-e2e               # Playwright E2E
pnpm test:python                       # API lint + typecheck + tests (ruff/mypy/pytest)
pnpm nx affected -t lint test build    # only what changed (base: main)
pnpm knip                              # unused files / deps / exports
```

Per-project targets are inferred by Nx — run `pnpm nx show project wyrdfold` to
list them. Remote build caching uses a Cloudflare R2 bucket via `@nx/s3-cache`
(set the R2 credentials in the environment to enable it; falls back to local
cache otherwise).

## Database (Supabase)

Migrations live in [`supabase/migrations`](./supabase/migrations). Common flows:

```bash
pnpm db:push        # apply local migrations to the linked project
pnpm db:gen-types   # regenerate apps/wyrdfold/src/lib/supabase/types.ts
```

## Deployment

- **Web** → Vercel (`apps/wyrdfold/vercel.json`). Needs `NEXT_PUBLIC_SUPABASE_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_ID`, and `WYRDFOLD_API_URL`.
- **API** → Railway (`apps/wyrdfold-api/Dockerfile` + `railway.toml`). Needs
  `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and provider keys (see
  `apps/wyrdfold-api/.env.example`).

## Conventions

- A husky pre-commit hook runs `lint-staged` (ESLint + Prettier) and, when TS
  files are staged, `pnpm typecheck`.
- `main` is the trunk and the default base for `nx affected`.
