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
   between the merged PRs that no single PR could surface. **Work to DISPROVE
   the release, not to document that it works**: name the 2–4 interaction
   failures that could plausibly arise only once the PRs are combined, and
   build probes specifically designed to trigger each one (the #972 gate's
   examples: browse mode re-opening the dead-pagination click a sibling PR had
   closed; garbage queries falling through into browse-everything). Record what
   you validated and the residual risk in the PR body — and **lead the body
   with a gate-verdict block** so "can I merge?" is answerable at a glance:

   > Release blockers found: … / Required pre-merge gate: … /
   > Residual risks accepted: …

   Wording discipline in the report: scoped security probes are "boundary and
   abuse-resistance checks", never "abuse testing" (which implies broader
   adversarial work than ran); state exposure changes precisely ("remains
   bounded to the same depth and per-IP limits; X makes that bounded window
   directly browsable"), never as flat "unchanged" when the ease of access
   moved. **If the release includes any dependency PR, add a dependency-delta
   audit line** verified against the release diff itself — only the intended
   packages moved, zero collateral resolution churn (the #970 draft silently
   moved four unrelated packages; ordinary suites never catch this class).

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
   git-connected to `main`). **Derive the FE/API deploy ORDER per release from
   the contract change**, not from precedent — choose the order that keeps the
   intermediate mixed-version state compatible: if the new FE works with the
   old API, FE-first is safe (the 2026-08-19 release); if the old FE works
   with the new API, API-first is safe (the 2026-09-03 release, whose new FE
   sent a request shape the old API rejected). If neither mixed state is
   compatible, the release needs a compatibility bridge or a coordinated
   deployment — flag it, don't pick an order. Two
   things do **not** happen on their own:
   **(a) the frontend** — Vercel is _not_ git-connected; run
   `vercel --prod --build-env NEXT_PUBLIC_BUILD_SHA=$(git rev-parse HEAD)`
   from the repo root every release or the OLD frontend stays live against the
   new API (the #459 skew — `docs/decisions.md`). The `--build-env` flag is
   part of the command, not an option: it bakes the checkout's SHA into the
   FE artifact, and step 7's provenance check reads it back from
   `/api/version` (#976). **(b) the migrations** —
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
7. **After deploy, smoke the running prod app on the changed surface — and
   prove BOTH artifacts correspond to the release commit.** Env vars and
   secrets drift independently of code; no pre-merge check sees them. Hit the
   changed prod endpoints (authed where it matters) and watch the logs.
   Provenance is symmetric now (#976): the API's `/version` carries the
   Railway-injected SHA and the FE's `/api/version` carries the SHA baked in
   by step 5(a)'s `--build-env` flag — assert BOTH equal the merge commit. A
   `commit: null` from the FE means the deploy skipped the flag: provenance
   is unproven, so re-deploy with it rather than reasoning it away. A
   behavioral discriminator is still worth one probe — provenance proves
   which builds are live, while a changed-contract probe proves the two
   artifacts interoperate (the #972 smoke's blank-`q` probe returned 200 only
   if BOTH the new BFF and the new API were live — the old BFF 400'd it and
   the old API 422'd it). Why this step exists: `docs/decisions.md` →
   "disabled legacy anon key".

The gate proves the release is **correct** (tests + integration), **usable**
(real flows end-to-end), and **safe** (no widened abuse surface), and leaves
the code **better refactored** than it found it.
