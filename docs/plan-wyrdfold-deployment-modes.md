# Deployment Modes: Self-Host Owner + Secure Multi-Tenant SaaS

**Author:** Claude (2026-07-01)
**Status:** Epic / plan. **Foundational** — it resolves the single-tenant/multi-tenant
duality that currently blocks RLS adoption, and is the umbrella for #88 (RLS), #6
(tenant isolation), and #7 (multi-tenant readiness).

## Goal

One codebase, two deployment modes:

- **Self-host** — a developer runs their own instance. Single owner. Can read the code
  and contribute (FSL license). Simple to stand up, but still **secure** (RLS is on even
  here, so an exposed instance isn't wide open).
- **SaaS (owner-host)** — the maintainer runs a secure, multi-tenant, monetized instance
  for the general public.

These are **two deployment modes of one product**, not two products.

## The core principle: collapse the duality, don't maintain it

Today the app carries a single-tenant/multi-tenant _duality_ baked into the data path:
`user_id` is nullable, most user-facing endpoints use `get_current_user_id_optional`,
the services branch on `query.is_("user_id","null") if user_id is None else …`, and the
service-role client is used almost everywhere (bypassing RLS). That duality is exactly
what blocks the RLS migration (#88): `get_user_supabase` is JWT-only and RLS enforces
`auth.uid() = user_id`, so it can serve neither an api-key caller nor a `user_id = NULL`
("global") row.

Replace the duality with a single model:

> **Self-host is just multi-tenant with exactly one user — the owner.**

- Every user-data row **always** has a `user_id`.
- RLS (`auth.uid() = user_id`) is **always** on.
- A self-host instance has one account; the SaaS has thousands — but the **core data path
  is identical**.

Why this is the right call:

- **RLS becomes uniform** → #88/#6 turn into a clean migration instead of a special-case
  minefield.
- **A self-hosted instance is secure even if exposed to the internet** (RLS walls it). The
  current "no-auth single-tenant" mode is _not_ — that's a latent footgun for any dev who
  points a domain at their instance.
- **One code path** to test and maintain, instead of two half-tested ones.

## The mode flag: `DEPLOYMENT_MODE = self_host | saas`

A single config switch gates **only the perimeter** — never the data model:

| Concern                     | `self_host`                                                          | `saas` (paid host)                                               |
| --------------------------- | -------------------------------------------------------------------- | ---------------------------------------------------------------- |
| **Signup**                  | Closed — owner provisioned on first run (`OWNER_EMAIL` / setup flow) | Open — public sign-up + email verification                       |
| **Billing**                 | Off                                                                  | Stripe subscription / usage; quota gate                          |
| **LLM cost**                | Owner's own key (BYOK, #5) — they pay their own inference            | Tiered: free = BYOK required, paid = owner's keys + per-user cap |
| **Data model / RLS / auth** | **identical**                                                        | **identical**                                                    |

The mode is config + a little auth/billing middleware. The core (schema, RLS, the
per-user data path) does not fork.

## Self-host owner provisioning

- **First-run setup** creates the owner user — from an env `OWNER_EMAIL` (magic-link / set
  password) or a one-time setup screen.
- The owner **authenticates** like any user → gets a JWT → RLS applies to them. In a
  single-owner instance the owner simply _is_ the only `auth.uid()`, so "their data" is all
  the data — RLS is a no-op cost but a real safety net.
- Signup is closed in `self_host`; there is exactly one account unless the operator opens it.

## Monetization (saas mode)

- **BYOK is the cost firewall.** The free tier requires the user's own OpenRouter key
  (#5, already built) → their inference bills _them_, not the host. The host charges for the
  app / features / convenience, and only absorbs LLM cost on paid tiers (with the per-user
  caps that already exist). This keeps infra cost **sub-linear in users**.
- **The shared catalog scales cheaply.** A target is polled once and served to every user
  who follows it — shared `scores` with per-user overlays (#60). One expensive poll → many
  paying customers. That is the correct multi-tenant economics and the app already has it.
- **RLS is mandatory here, not optional.** For a _paid_ host serving the public, a Python-
  authz bug is a cross-customer data breach. RLS as the enforced backstop (#6/#88) is the
  thing that lets the operator charge money and sleep. So #88 is a **prerequisite for
  monetization**, not polish.
- **Billing:** Stripe subscription and/or usage-metered; a quota/entitlement gate on the
  expensive paths (analysis, tailor, derive).

## Licensing

The repo is **FSL-1.1-ALv2** (Functional Source License → Apache-2.0 after the term). That
is precisely the "let developers self-host and contribute, but nobody can stand up a
_competing paid host_ for the license window" model. So: keep the repo readable, accept
contributions, run the commercial SaaS with a protected moat. **No licensing change needed.**

## Phased epic

### Phase 0 — Groundwork (prerequisite; do while still single-user)

The DB surgery is only safe while there's effectively one user's data. Do it first.

- Provision an **owner user** on first-run; make auth **required** in both modes.
- **Backfill `user_id`** on existing `NULL` rows → the owner.
- Make `user_id` **`NOT NULL`** on the per-user tables.
- Remove the `is_("user_id","null")` branches + `get_current_user_id_optional` on user-data
  endpoints → require a JWT.
- **Validation:** every per-user table has `user_id` populated; the app works end-to-end as
  the authenticated single owner; no endpoint 500s for lack of the `NULL` path.

### Phase 1 — RLS migration (= #88 / #6)

- Migrate the **per-user** endpoints onto `get_user_supabase` (RLS-enforced): `experience_*`,
  `user_*`, `analyses`, `job_feedback`, `uploaded_resumes`, `documents`, `contribution_votes`,
  `llm_costs`, `status_log`, `target_learning_log`.
- Leave the **shared catalog** (`jobs`, `scores`, `targets` — read policy `true` by design,
  scoped per-user in Python via `user_targets`) and the **operator/cron/pre-auth** paths on
  the service-role client, **documented** as intentional.
- Some endpoints legitimately use **both** clients — a per-user read under RLS + a shared
  write via a SECURITY DEFINER RPC (the `analysis.py` dual-client pattern).
- **Validation:** each migrated endpoint gets a live-stack RLS integration test proving
  cross-user isolation (user A's JWT can't read/write user B's rows through it).

### Phase 2 — Mode flag + perimeter

- `DEPLOYMENT_MODE` config. `self_host`: closed signup, owner-provisioned, BYOK default.
  `saas`: open signup + email verification, onboarding for new tenants.

### Phase 3 — SaaS monetization

- Billing (Stripe), tiers (free = BYOK required, paid = host keys + quota), abuse controls,
  and the public onboarding funnel.

## Issue mapping

- **#88** → **Phase 1** (RLS migration). Re-scope from "adopt the built `get_user_supabase`"
  to "the RLS phase of deployment-modes"; it is **blocked on Phase 0** (can't RLS-migrate
  endpoints that still serve `user_id = NULL` / api-key callers).
- **#6** (tenant isolation via RLS) → the umbrella for Phase 1.
- **#7** (multi-tenant readiness) → this whole epic.
- **#5** (BYOK) → the cost firewall for the SaaS free tier (done; leveraged in Phase 3).

## Acceptance criteria

- One codebase; `DEPLOYMENT_MODE` switches `self_host` ↔ `saas` with **no data-model
  divergence**.
- Every per-user table: `user_id NOT NULL` + RLS enforced; cross-user isolation proven by
  integration tests.
- **Self-host:** owner-provisioned, authenticated, RLS-walled even if exposed to the net.
- **SaaS:** open signup + billing + BYOK free tier; per-user quota; RLS backstop.

## Risks / notes

- The **backfill + `NOT NULL`** is the risky DB step — irreversible-ish and touches every
  per-user table. Do it **while single-user** (one owner's data), snapshot first, and gate it
  behind the migration-safety guard.
- The **shared catalog** (`jobs`/`scores`/`targets`) stays shared — genuinely shared data
  (many users follow one target; scores are cached). Do **not** try to RLS-isolate it;
  per-user scoping is application-level via `user_targets`, and that is correct.
- Sequence matters: **Phase 0 → 1 → 2 → 3**. Retrofitting isolation onto a _live_ multi-tenant
  DB (real paying users' data already interleaved) is far riskier than doing it now, while the
  maintainer is still the only user.
