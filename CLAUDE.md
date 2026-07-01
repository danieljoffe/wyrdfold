# CLAUDE.md

Conventions for AI coding agents (and humans) working in this repo. Kept lean — this
loads into every session.

## Working rhythm — always propose the next move

Don't end a turn asking whether to stop. There's always worthwhile work; the job is to
pick the _right_ next thing and name it. When a piece of work lands, propose the next step
as a short paragraph — **what, why now, and roughly how** — and proceed unless redirected.
Deliberately alternate two lanes:

- **Build** — the next feature or fix.
- **Tend** — refactor, restructure, delete, or rethink an approach the code has outgrown.
  Software is a living set of instructions; it needs periodic revision to stay in working
  order.

Feature work alone never finishes (a dog chasing its tail), so Tend is a first-class
choice, not filler — pivot to it deliberately after a run of Build work. The
proposal-paragraph keeps this honest: it has to justify _why this next_, not "there's
always more."

## Validate and stress-test before opening a PR

A PR ships **already-proven**, not "tests to follow." Before `gh pr create`:

- **Run the real checks** for what you touched — tests + lint + typecheck, green. Not a
  narrow smoke.
- **Exercise negative and edge cases**, not just the happy path. A guard you add should be
  shown to fail when it should (a regression test that actually catches the regression; a
  validator that rejects the bad input).
- **Validate against real data or a realistic fixture** where feasible — watch it actually
  run; don't just confirm it imports/compiles.
- **State what you validated in the PR body** — what ran, what you couldn't test, and the
  residual risk.

See also `CONTRIBUTING.md` → "Before opening a PR" and "Touching prompts or scoring code".

## Releases are the pause point — and an integration gate

"Create a release" / "open a PR from `develop` → `main`" is the deliberate checkpoint in
the working rhythm above. When asked:

1. **Finish or cleanly park** the work in flight first, so the release captures a coherent
   state.
2. **Review the release itself** — don't just open the merge PR. Read the full
   `develop`→`main` diff and run the pre-PR bar above ("Validate and stress-test") against
   the _whole_ release: tests + lint + typecheck green, negative/edge cases, and validation
   against real data / a realistic fixture — hunting especially for interactions between the
   merged PRs that no single PR could surface. Record what you validated and the residual
   risk in the release PR body.

The release PR is where accumulated changes earn their integration-level proof — a gate,
not a rubber stamp.

## Repo & PR governance

- **Base branch:** open PRs against `develop`, not `main`. `main` is release-only
  (`develop` → `main`); `.github/workflows/pr-base-branch.yml` fails PRs opened
  against `main` from anything but `develop` / `release/*` / `hotfix/*`.
- **Sign automated comments.** `gh` posts as the repo owner, so when an agent
  authors an issue/PR comment, sign it (e.g. "— Claude (Claude Code)") so it's
  distinguishable from a human-authored one.
- **Reading CI as an agent:** the default `GITHUB_TOKEN` 403s on Actions reads;
  use `env -u GITHUB_TOKEN gh …` (keychain auth) to watch checks / read job logs.
