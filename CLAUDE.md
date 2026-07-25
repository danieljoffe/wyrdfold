# CLAUDE.md — wyrdfold

Repo-specific conventions. The machine-wide engineering rules
([github.com/danieljoffe/claude-rules](https://github.com/danieljoffe/claude-rules),
loaded into every session via `~/.claude/CLAUDE.md`) already cover the **working rhythm**,
**durable-over-quick / surface-the-fork**, **prove-the-diagnosis**, **validate-before-PR**,
**review-before-merging**, and the **`develop`/`main` + `gh`** basics — not repeated here.
This file adds only what's specific to wyrdfold, and wins on conflict.

## Releases — the pause point and integration gate

"Create a release" / "open a PR from `develop` → `main`" is the deliberate checkpoint in
the working rhythm. When asked:

1. **Finish or cleanly park** the work in flight first, so the release captures a coherent
   state.
2. **Review the release itself** — don't just open the merge PR. Read the full
   `develop`→`main` diff and run the full validate-before-PR bar (general rules) against
   the _whole_ release — hunting especially for interactions between the merged PRs that no
   single PR could surface. Record what you validated and the residual risk in the PR body.
3. **Exercise the running system, not just the suite.** Green tests prove the pieces; they
   don't prove the assembled app works for a user or that the API is hard to abuse. Scoped to
   what the release touched: **drive the real app** (browser) through the changed user
   journeys — the interaction works end-to-end (real clicks → API round-trips → render), not
   just that a component unit-renders — and **probe the changed API surface** for abuse (authz
   refuses a non-owner, malformed / oversized / injection input is rejected, rate-limit +
   cost-bearing paths, IDOR, PII/error leakage). Keep it proportional — the flows/endpoints
   the release changed, not a full regression or pen-test; a docs-only release skips it.
4. **Act on what the review surfaces — in a separate PR.** Reading the whole release at once
   (and using it) exposes what no single PR could: cross-PR duplication, an abstraction the
   stacked PRs outgrew, a refactor that's now obvious. Open a **new PR into `develop`** with
   those cleanups for review — never fold them into the release PR, which must keep shipping
   the _exact_ state you just proved. It rides the next release and doesn't block this one. (A
   genuine **bug** the review catches is different: it doesn't ride forward — fix it on
   `develop` and re-run the gate before merging.)
5. **A merge is not a deploy — ship the frontend, then the migrations.** Merging
   `develop → main` auto-deploys only the **API** (Railway is git-connected to `main`).
   Two things do **not** happen on their own. **(a) The frontend:** Vercel is _not_
   git-connected, so run `vercel --prod` from the repo root every release, or a merge
   leaves the OLD frontend live against the new API — the skew that bit #459, so deploy the
   frontend and the API together. **(b) The migrations:** **nothing applies
   `supabase/migrations/` to prod**. A release containing migrations is not done until they
   are applied to the prod DB and **verified**: `list_migrations` shows the new versions and
   the invariants they create (policies, constraints, backfills) spot-check true. Apply
   immediately around the merge — migrations are written to be backward-compatible with the
   running code, so migrations-first is the safe order. This step exists because three
   releases once shipped RLS code whose policies never reached prod, breaking status writes
   live.
6. **After deploy, smoke the running prod app on the changed surface.** Green CI + applied
   migrations still don't prove prod's _environment_ is right — env vars and secrets drift
   independently of code and no pre-merge check sees them. Hit the changed prod endpoints
   (authed where it matters) and watch the logs. This step exists because a release flipped
   `GET /jobs` onto the RLS user client whose prod `SUPABASE_ANON_KEY` was a **disabled
   legacy key** — 500-storming the hottest endpoint, invisible to every local/CI check
   (which used a working local key). The API now boot-guards its Supabase keys
   (`_probe_supabase_keys`), but the general lesson stands: prod config is only proven by
   touching prod.

The release PR is a gate, not a rubber stamp — the step proves the release is **correct**
(tests + integration), **usable** (the real flows work end-to-end), and **safe** (no widened
abuse surface), and leaves the code **better refactored** than the release found it.

## Validating this repo (beyond the general PR bar)

- **Live API validation runs the Docker image.** When validation means _running_ the API
  (release-gate drives, endpoint probes), run the containerized API — the same image CI's
  Trivy job builds and Railway deploys — pointed at the local stack
  (`SUPABASE_URL=http://host.docker.internal:54321` on macOS). Host `uvicorn` misses
  packaging/startup issues (env requirements, pandoc, Python drift) that only the artifact
  shows. Plain pytest suites stay on the host venv — they don't boot the API.
- **Grow the LLM mock with every PR that touches LLM surfaces.** A PR touching LLM calls,
  prompts, or LLM-output parsing must extend the mock's edge battery for the surface it
  touched (malformed/truncated JSON, fenced output, schema-violating payloads, empty
  content, injection-looking text echoed as data, mid-stream provider errors, …). Every
  LLM bug we hit becomes a named mock behavior + regression test — the mock is the
  accumulated bug corpus, so new endpoints inherit every past failure mode for free.

See also `CONTRIBUTING.md` → "Before opening a PR" and "Touching prompts or scoring code".

## Repo governance specifics

- `.github/workflows/pr-base-branch.yml` fails a PR into `main` from anything but
  `develop` / `release/*` / `hotfix/*` (so a hotfix may target `main` directly).
