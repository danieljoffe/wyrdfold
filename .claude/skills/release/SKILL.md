---
name: release
description: Cut a wyrdfold release — the develop→main integration gate, deploy, and prod smoke, end to end
user-invocable: true
---

# Release — the pause point and integration gate

"Create a release" / "cut a release" / "open a PR from `develop` → `main`" is
the deliberate checkpoint in the working rhythm. Run every step; the PR is a
gate, not a rubber stamp.

1. **Finish or cleanly park** the work in flight first, so the release captures
   a coherent state.
2. **Review the release itself** — don't just open the merge PR. Read the full
   `develop`→`main` diff and run the full validate-before-PR bar (general
   rules) against the _whole_ release — hunting especially for interactions
   between the merged PRs that no single PR could surface. Record what you
   validated and the residual risk in the PR body.
3. **Exercise the running system, not just the suite.** Green tests prove the
   pieces; they don't prove the assembled app works for a user or that the API
   is hard to abuse. Scoped to what the release touched: **drive the real app**
   (browser) through the changed user journeys — real clicks → API round-trips
   → render, on BOTH the logged-out and authed surfaces when both changed (the
   authed drive is what caught the salary-filter BFF drop, #531) — and **probe
   the changed API surface** for abuse (authz refuses a non-owner,
   malformed/oversized/injection input is rejected, rate-limit + cost-bearing
   paths, IDOR, PII/error leakage). Use the Docker image for API drives (see
   `.claude/rules/api-validation.md`). Keep it proportional — the
   flows/endpoints the release changed; a docs-only release skips it.
4. **Act on what the review surfaces — in a separate PR.** Cross-PR
   duplication, an outgrown abstraction, an obvious refactor: open a **new PR
   into `develop`** — never fold cleanups into the release PR, which must ship
   the _exact_ state you proved. (A genuine **bug** is different: fix it on
   `develop` and re-run the gate — or, if already deployed, ship the fix as an
   immediate follow-up release.)
5. **A merge is not a deploy — ship the frontend, then the migrations.**
   Merging `develop → main` auto-deploys only the **API** (Railway is
   git-connected to `main`). Two things do **not** happen on their own:
   **(a) the frontend** — Vercel is _not_ git-connected; run `vercel --prod`
   from the repo root every release or the OLD frontend stays live against the
   new API (the #459 skew — `docs/decisions.md`). **(b) the migrations** —
   apply and **verify** around the merge, migrations-first (they're written
   backward-compatible); see `.claude/rules/api-validation.md` for the
   version-stamp gotcha and `docs/decisions.md` → "RLS policies never reached
   prod" for why.
6. **If the release touches AI providers, sub-processors, or what leaves the
   system, re-verify the privacy claims before shipping.** The Terms and
   Privacy Policy make specific, checkable statements — Zero-Data-Retention and
   training opt-out on the OpenRouter account, which sub-processors receive
   what, that the embedding provider gets no contact details or account
   identifier, that BYOK is not offered on the hosted service. These live in
   third-party dashboards and in code paths that move; a published policy that
   has drifted from the system is a misrepresentation, not a stale doc. Two of
   these were found FALSE during the #439 legal review (`docs/decisions.md`),
   which is why this step exists. `pytest tests/test_embedding_pii_boundary.py`
   and the `legalPages.spec.tsx` guards cover the code side; the provider
   account settings must be eyeballed by whoever holds them.
7. **After deploy, smoke the running prod app on the changed surface.** Env
   vars and secrets drift independently of code; no pre-merge check sees them.
   Hit the changed prod endpoints (authed where it matters) and watch the
   logs. A version discriminator helps: probe something only the new code does
   (e.g. a new param that 422s) to prove the deploy actually cut over. Why
   this step exists: `docs/decisions.md` → "disabled legacy anon key".

The gate proves the release is **correct** (tests + integration), **usable**
(real flows end-to-end), and **safe** (no widened abuse surface), and leaves
the code **better refactored** than it found it.
