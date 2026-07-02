# wyrdfold-e2e

Playwright suite for the wyrdfold app. Two tiers of specs:

| Tier   | Project                                                       | Specs                                 | Auth needed?           |
| ------ | ------------------------------------------------------------- | ------------------------------------- | ---------------------- |
| Public | `public-chromium` (+ `public-firefox`, `public-webkit` local) | `login.spec.ts`, `middleware.spec.ts` | No                     |
| Authed | `authed-chromium`                                             | `onboarding.spec.ts` (more to come)   | Yes — Supabase session |

The authed tier depends on a one-shot `auth.setup.ts` that writes a signed-in storage state to `src/.auth/user.json`. Auth setup **skips entirely** when the four env vars below are absent, so the public tier still runs cleanly in CI without secret plumbing.

## Running locally

### One-time: create the e2e test user

This is a real Supabase user — create it once via the dashboard (Authentication → Users → Invite user) or via the existing `pnpm --filter @danieljoffe.com/wyrdfold invite-beta <email>` script (which also seeds the `wyrdfold_beta_invites` allowlist row so the `before-user-created` auth hook lets the row through). Pick something memorable like `e2e@wyrdfold.test`. No profile data required — specs that need data should seed it themselves.

The fixture authenticates by minting an OTP via `auth.admin.generateLink({type:'magiclink'})` and exchanging it via `verifyOtp` — same code path as a real user clicking the magic-link, but without the inbox round-trip.

### Per-machine: set the env

In `apps/wyrdfold/.env.local` (the dev server picks it up; Playwright inherits via `webServer.env`):

```bash
E2E_TEST_USER_EMAIL=e2e@wyrdfold.test
# Already in your .env.local for dev; the e2e fixture needs it too
SUPABASE_SERVICE_ROLE_KEY=<service-role-key-from-supabase-dashboard>
```

The existing `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_ID` are already required for the dev server — Playwright reuses them.

### Run

```bash
# Run only the authed specs (auth setup chains in automatically)
pnpm nx e2e wyrdfold-e2e -- --project=authed-chromium

# Run only the public specs (no auth needed)
pnpm nx e2e wyrdfold-e2e -- --project=public-chromium

# Run everything (default)
pnpm nx e2e wyrdfold-e2e
```

## Running in CI

The `e2e` job in `.github/workflows/ci.yml` runs the **full tier with zero
secrets**: it boots a throwaway local Supabase stack (`supabase start` — the
same pattern as the `rls-integration` job), boots `wyrdfold-api` against it
with the mock LLM, seeds the test user per-run
(`scripts/seed-e2e-user.mjs`), and exports the four auth env vars from the
stack's well-known dev keys. The playwright config's `AUTH_ENABLED` gate then
registers `setup` + `authed-chromium`, so every push exercises the signed-in
routes against the real assembled stack (Next → wyrdfold-api → Supabase).

Nothing here touches the production Supabase project, and no repo secrets
are required — deliberate for a public repo, where Actions secrets are a
fork-PR risk surface. If any of the env plumbing is removed, the authed tier
silently self-skips (that was the pre-2026-07 state — treat a run whose
authed specs didn't execute as a coverage regression, not a pass).

## Why OTP via service role (and not password sign-in)

The wyrdfold app's login UI is magic-link only (`signInWithOtp`). A password-sign-in fixture would diverge from the production auth path and only work if the test user happened to have a password set in addition to OTP, which is fragile.

`auth.admin.generateLink({type:'magiclink'})` returns the email OTP token without sending an email; `verifyOtp` then exchanges it for a real session. Same code path as a real user clicking the magic-link in their inbox. In CI the service-role key involved is the local dev stack's public default — no production credential exists in the pipeline.

## Adding a new authed spec

1. Drop the new spec file at `src/<feature>.spec.ts`.
2. Add its filename to the `testMatch` regex of the `authed-chromium` project in `playwright.config.ts`.
3. The spec inherits the storageState — no per-spec setup needed.

If a spec needs deterministic data state (e.g., "user has no targets" or "user has exactly one resume_ready job"), wipe and seed inside the spec or via `apps/wyrdfold-api/scripts/wipe_user_data.py <user_id>` before the run.
