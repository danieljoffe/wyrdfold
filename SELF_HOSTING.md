# Self-hosting WyrdFold

WyrdFold runs in two modes from one codebase (`docs/plan-wyrdfold-deployment-modes.md`):
`self_host` (this guide — closed signup, you are the owner) and `saas` (the hosted
instance). The mode gates only the perimeter; the data model, RLS, and auth are
identical in both.

## What you need

- A **Supabase project** (hosted free tier works) — or the local stack via
  `supabase start` for kicking the tires.
- Somewhere to run the **API container** (`apps/wyrdfold-api/Dockerfile`; listens on
  `8001`) and the **Next.js frontend** (`apps/wyrdfold`).
- An **LLM key** — your own OpenRouter (or Anthropic) key. Self-host is BYOK by
  construction: your instance, your key, your inference bill.

## First run

1. **Apply the schema:** link the repo to your Supabase project and run
   `supabase db push` — every migration, including RLS policies and the
   closed-signup hook function, ships in `supabase/migrations/`.
2. **Configure the API** (see `apps/wyrdfold-api/.env.example` for the full list):

   ```bash
   DEPLOYMENT_MODE=self_host        # the default — shown for clarity
   OWNER_EMAIL=you@example.com      # first-run owner bootstrap
   SUPABASE_URL=...                 # your project URL
   SUPABASE_ANON_KEY=...            # publishable (sb_publishable_...) key
   SUPABASE_SERVICE_ROLE_KEY=...    # secret (sb_secret_...) key
   ALLOWED_HOSTS=your.api.host
   LLM_PROVIDER=openrouter
   OPENROUTER_API_KEY=...
   ```

   Boot the API. At startup it validates its config (it fails loudly on missing or
   disabled keys rather than 500ing later) and **provisions your owner account**:
   the `OWNER_EMAIL` auth user is created automatically — watch for
   `owner_provisioning: created owner ...` in the deploy log. Idempotent; reboots
   are no-ops.

3. **Configure the frontend** (see `apps/wyrdfold/.env.example`):

   ```bash
   NEXT_PUBLIC_SUPABASE_URL=...       # same project URL as the API
   NEXT_PUBLIC_SUPABASE_ANON_ID=...   # same publishable key
   WYRDFOLD_API_URL=https://your.api.host
   NEXT_PUBLIC_DEPLOYMENT_MODE=self_host   # homepage shows sign-in, not the waitlist
   ```

4. **Sign in:** open the app, enter `OWNER_EMAIL` in the magic-link form, click the
   link in your inbox. There are no passwords anywhere — auth is magic-link only.

## Closed signup — and letting someone else in

Signup is **closed by default**: a `before-user-created` auth hook (wired in
`supabase/config.toml`, function shipped in migrations) rejects any self-service
signup whose email isn't in the `wyrdfold_beta_invites` table. Your owner account
is unaffected (admin-API creation bypasses the hook — that's how boot provisioning
works).

To invite another person, add their email to the allowlist:

```sql
insert into wyrdfold_beta_invites (email) values ('friend@example.com');
```

They then sign in through the normal magic-link form. Every user is isolated by
Postgres RLS — the whole per-user data path is enforced at the database, not just
in application code (`docs/rls-data-access.md`).

> **Hosted-Supabase note:** the hook wiring in `supabase/config.toml` applies to
> stacks managed by the CLI (local / `supabase start`). On a hosted Supabase
> project, enable the hook once in the dashboard: Authentication → Hooks →
> Before User Created → Postgres function → `public.hook_restrict_wyrdfold_beta`.

## Day 2

- **Upgrades:** pull, `supabase db push` (migrations first), redeploy the API
  image, redeploy the frontend.
- **Backups:** enable them in your Supabase project settings.
- **Cost control:** the API enforces per-user LLM budget caps (`llm_costs` ledger);
  your own key means your own spend either way.
