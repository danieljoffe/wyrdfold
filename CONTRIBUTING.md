# Contributing to WyrdFold

Thanks for taking the time! WyrdFold is small and opinionated — read
this once before sending a PR and we'll waste less of each other's time.

## Reporting bugs / asking questions

Open an issue using one of the templates. The "self-host environment"
field really helps — most reproductions hinge on which Supabase project,
which feature flags, which providers you have wired up.

If you've found a **security** issue, see
[`SECURITY.md`](./SECURITY.md) — don't open a public issue.

## Dev setup

The README has the [self-host
quickstart](./README.md#self-hosting-quickstart). For development you
want the same flow plus dev deps:

```sh
pnpm install        # installs JS + triggers `uv sync` for Python
pnpm dev            # next dev on :3100 + API on :8001
```

You'll want:

- **Node 24** (`.nvmrc` pins this)
- **pnpm** (the lockfile is frozen; CI uses `--frozen-lockfile`)
- **uv** (Python deps; the postinstall hook runs `uv sync` if it finds it)
- **Python 3.11** (matches the production Dockerfile)
- **pandoc** (DOCX render tests shell out to it)

## Before opening a PR

```sh
pnpm pom            # typecheck + lint:fix + format + test + python suite
```

CI will run:

- `pnpm nx run-many -t lint typecheck test build --exclude=wyrdfold-api`
- Public Playwright specs (`apps/wyrdfold-e2e/src/login.spec.ts`,
  `middleware.spec.ts`) against a built production server
- Python: `ruff` + `mypy --strict` + `pytest` for `apps/wyrdfold-api/`
- `pnpm audit --prod` (production-tree advisories must stay at zero)

Authed Playwright specs stay local-only — they need real Supabase
service-role creds.

## Database migrations

Migrations live in `supabase/migrations/` and are **forward-only** — never
edit one after it merges; add a new migration instead.

**Destructive DDL is guarded ([#109]).** `DROP COLUMN`, `DROP TABLE`, and
`TRUNCATE` are irreversible: if the backfill they depend on is wrong, the
row data is gone with no recovery path. A pytest check
(`apps/wyrdfold-api/tests/test_migration_safety.py`) fails CI on any _new_
migration that introduces one without a deliberate guard. To add a
destructive migration:

1. **Snapshot first**, in the same migration —
   `CREATE TABLE _jobs_status_backup AS SELECT id, status FROM jobs;`
2. **Verify the backfill row-counts match** before the drop.
3. Keep the drop in its **own clearly-labeled migration**, decoupled from
   the backfill it depends on.
4. Add a `-- guarded-destructive: <why it's safe / where the snapshot is>`
   comment so the check passes and reviewers see the acknowledgement.

[#109]: https://github.com/danieljoffe/wyrdfold/issues/109

## PR conventions

- **Title**: imperative, scope-prefixed, under ~70 chars
  (`fix(wyrdfold-api): …`, `chore(deps): …`, `perf(ci): …`).
- **Body**: use the PR template. Fill in the test plan; "manually
  verified in browser" is fine when relevant.
- **Tests**: every behavior change needs a regression test. The bar
  isn't 100% coverage — it's "if this regresses, what catches it?"
- **One concern per PR.** A dep bump and a refactor go in separate PRs.

## Touching prompts or scoring code

WyrdFold's product promise is match quality, so prompt-affecting
changes get an extra bar (see
[#27](https://github.com/danieljoffe/wyrdfold/issues/27) for the eval
regression audit). If your change touches anything in:

- `apps/wyrdfold-api/app/services/relevance/`
- `apps/wyrdfold-api/app/services/fit/`
- `apps/wyrdfold-api/app/services/analysis/`
- `apps/wyrdfold-api/app/services/targets/derive_profile*.py`
- `apps/wyrdfold-api/app/services/tailor/prompts*.py`

…please run the relevant eval script in `apps/wyrdfold-api/scripts/`
and attach a before/after summary to the PR. The eval scripts live in
that directory; pick the one closest to the prompt you touched.
Reviewers will ask for this if you don't volunteer it.

## Typography (system fonts only)

WyrdFold uses the platform's system font stack — no `next/font`, no
webfont CDN. Tailwind v4's default `font-sans` (`ui-sans-serif`,
`system-ui`, …) is what every text style resolves to. Reasons it stays
that way:

- **CWV:** no extra request, no font-swap layout shift. The site
  renders with whatever font the OS already loaded.
- **Self-hoster simplicity:** no Google Fonts opt-in, no GDPR concerns,
  no CDN account to fund.
- **Look and feel:** the brand accents are colour + spacing + the
  WyrdFold mark, not a custom face. SF Pro on macOS / Segoe UI on
  Windows / Roboto on Android all read well at the sizes we use.

If you have a strong reason to introduce a webfont, open an issue
first — we'd want to see the CWV trade-off and the licensing path
spelled out before adding a font dependency to a self-hostable app.

## License + DCO

By contributing you agree that your contribution is licensed under
[FSL-1.1-ALv2](./LICENSE.md) (the project's license, which becomes
Apache-2.0 per release after two years).

No CLA, no formal DCO check — just don't paste in code you can't
license.
