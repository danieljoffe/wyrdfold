# Production cutover — building a clean prod, demoting the current stack to staging

Everything named "dev" today **is** production: Supabase project _"WYRDFOLD
dev"_ (`swxiuutaikxbirauivjg`) and Railway project _"DEV:wyrdfold-api"_,
environment _"development"_, are what wyrdfold.com runs on. That naming is a
live footgun — it has already misled both operator and agent.

The plan inverts it: relabel the current stack as **staging**, stand up a
**new Supabase project as production**, and seed it with the shared catalog
only. Launch then starts from zero user data.

Everything below marked ✅ was verified on 2026-08-20, not assumed.

## What is already proven

✅ **All 131 migrations apply cleanly to an empty database.** Run via
`supabase db reset` against a wiped local stack — the same path a brand-new
project takes. This was the largest unknown in the plan.

✅ **The result is complete and safe**, not merely error-free:

| Check                         | Result               |
| ----------------------------- | -------------------- |
| public tables                 | 37                   |
| RLS-enabled tables            | **37 — all of them** |
| RLS policies                  | 28                   |
| storage buckets               | 2, both **private**  |
| `hook_restrict_wyrdfold_beta` | present              |
| entitlement pin trigger       | present              |
| `wyrdfold_beta_invites`       | present              |

✅ **Storage buckets ship in migrations** (`20260614130000_storage_rls.sql`
inserts into `storage.buckets`), so they are not a manual step that gets
forgotten.

✅ **Only one place hardcodes the project ref** — `supabase/config.toml`, which
this plan rewrites anyway. No code change needed.

✅ **The API needs no JWT secret.** It verifies via JWKS at
`{SUPABASE_URL}/auth/v1/.well-known/jwks.json` (`dependencies.py:242`), so
changing `SUPABASE_URL` repoints key verification automatically.

## What to copy, and what must not move

Measured row counts on the current database:

| Table            |    Rows | Copy?                          | Why                                |
| ---------------- | ------: | ------------------------------ | ---------------------------------- |
| `sources`        |   4,533 | **yes**                        | the expensive board-discovery work |
| `jobs`           |  63,310 | **yes**                        | the corpus                         |
| `job_embeddings` |   8,526 | **yes**                        | real Voyage spend to regenerate    |
| `targets`        |      19 | **yes, without `description`** | see below                          |
| `scores`         | 389,806 | **NO**                         | see below                          |

**`scores` must not move.** It looks like catalog data because erasure never
deletes those rows — but `fit_reasoning`, `axis_scores` and `logistics_filters`
are derived from a specific user's résumé, and `fit_reasoning` quotes named
employers (`account_deletion.py:108`). That is why erasure _scrubs_ those
fields rather than dropping the row. Copying the table would carry across
exactly the personal data that machinery exists to remove. They are also
target-scoped, so they are meaningless in a prod with no users, and regenerate
on the first poll.

**`targets.description` should not move either**, even though it is currently
clean — 19 rows, 16 with descriptions, **0 second-person** (checked; #868's fix
held). With only 19 rows it costs nothing to copy `label`, `scoring_profile`,
`search_keywords` and `app_active` and let descriptions regenerate. Cheaper
than proving 16 free-text fields carry no employer names.

## Existing accounts

7 auth users total — this is not a user base:

| Domain                |   n | Last sign-in                             |
| --------------------- | --: | ---------------------------------------- |
| `danieljoffe.com`     |   1 | 2026-08-18 — owner                       |
| `dannys.io`           |   2 | 2026-08-21 — owner's test accounts       |
| `melissajoffe.com`    |   1 | 2026-07-01                               |
| `gmail.com`           |   1 | 2026-06-06 — one session, never returned |
| `piotr-kuczynski.com` |   1 | 2026-06-03 — one session, never returned |
| `wyrdfold.internal`   |   1 | never — service account                  |

So the decision is about **two dormant external testers**: email them to
re-register on the new prod, or delete their accounts. Leaving real career data
in a system newly relabelled "staging" without telling them is the one option
that is not defensible.

## Environment variables

**Railway API — 9 change, 47 carry over unchanged.**

- New Supabase project: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
  `SUPABASE_SERVICE_ROLE_KEY`
- Stripe live mode (#861): `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `STRIPE_STARTER_PRICE_ID`, `STRIPE_PRO_PRICE_ID`
- Front end: `NEXT_APP_URL`, `ALLOWED_HOSTS`

**Vercel — 4 change, 6 carry over.**

- New Supabase project: `NEXT_PUBLIC_SUPABASE_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_ID`, `SUPABASE_SERVICE_ROLE_KEY`
- New API service: `WYRDFOLD_API_URL`

`WYRDFOLD_BFF_SECRET`, `WYRDFOLD_CRON_KEY` and `CRON_SECRET` are shared secrets
that must stay **identical on both sides**; carry them over rather than
regenerating, or regenerate both together.

## Stripe live-mode traps

- **Live Price IDs are different objects from test Price IDs.** Products and
  prices must be recreated in live mode; the existing `price_…` values will not
  resolve. This is the substance of #861 — it is not a single toggle.
- **The webhook secret is per-endpoint.** A new live-mode endpoint pointing at
  the new API URL produces a new signing secret.
- Staging keeps test keys permanently. That separation is the point: it is what
  makes taking production live safe to rehearse.

## Sequence

Only step 4 is a cutover, and it is reversible by reverting env vars.

1. Create the new Supabase project (`us-east-2`, matching the current region so
   Railway latency is unchanged). **The operator runs this** — it takes a
   `--db-password`, which should not pass through an agent or a transcript.
2. `supabase db push` the full migration history to the new project.
3. Rewrite `config.toml`: current ref becomes `[remotes.staging]`, new ref
   becomes `[remotes.production]`. Push auth config to **both** so the email
   templates exist on each.
4. Copy the four catalog tables.
5. New Railway environment on the new Supabase, live Stripe keys. The existing
   environment keeps test keys and becomes staging.
6. Repoint Vercel production env vars, deploy, verify against the new stack.
   **← the cutover**
7. Point `staging.wyrdfold.com` at staging. Note Vercel env vars scope to
   _environment_, not domain — two domains on one project share production
   vars, so staging needs a second Vercel project or the Preview environment.
8. Rename the old Supabase project and Railway environment so "dev" stops
   meaning prod.

## Post-cutover verification

Reuse what the release gate already does, against the new stack:

- `GET /version` on the new API equals `main` HEAD — proves the cutover, not
  just a healthy old service
- signed-out `curl` of `/dashboard`, `/jobs`, `/settings` still 307s to
  `/login`; `/logo.png` and the touch icons still return 200 with image types
  (#899)
- an invite end-to-end, opened on a **different device** than the one that
  requested it — the only test that proves the `token_hash` path (#856, #860)
- `GET /targets/mine` returns 200 for an unsubscribed user while
  `POST /targets/suggest` still 402s (#893)

## When `signup_mode` flips to open

The public surfaces adjust in two ways — one automatic, one not:

- **Automatic:** `/login` drops the "Not invited yet? Join the waitlist" link
  and the closed-beta warning on its own (it probes `/api/signup-mode`, #963).
- **Manual copy pass (#971 §3):** the public CTAs deliberately read
  **"Get early access"** while signup is closed (`PublicSearchHeader` and the
  listing-detail upsell in `ListingDetailBody`) — restore **"Sign up free"**
  when the promise becomes true, and update the `JobDetailModal` spec's
  link-name assertions in the same commit.
