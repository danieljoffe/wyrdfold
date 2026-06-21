# CLAUDE.md

Conventions for AI coding agents (and humans) working in this repo. Kept lean — this
loads into every session.

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
