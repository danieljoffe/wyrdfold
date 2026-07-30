---
paths:
  - 'apps/wyrdfold-api/**'
  - 'supabase/migrations/**'
---

# Live API validation runs the Docker image

When validation means _running_ the API (release-gate drives, endpoint probes),
run the containerized API — the same image CI's Trivy job builds and Railway
deploys — pointed at the local stack
(`SUPABASE_URL=http://host.docker.internal:54321` on macOS). Host `uvicorn`
misses packaging/startup issues (env requirements, pandoc, Python drift) that
only the artifact shows. Plain pytest suites stay on the host venv — they don't
boot the API.

Caveat: the container cannot serve **authed** local-browser traffic (JWT `iss`
mismatch: `host.docker.internal` vs `127.0.0.1`). Use host `uvicorn` for authed
browser drives; the container covers the artifact/operator/public surface.

# Migrations are manual and verified

Nothing applies `supabase/migrations/` to prod automatically. A migration ships
only when applied to the prod DB **and verified** (`list_migrations` shows the
version; the invariants it creates spot-check true). The MCP `apply_migration`
stamps its own timestamp — UPDATE `supabase_migrations.schema_migrations` to
the repo's version after applying, or future CLI diffs see the migration as
pending. Why this rule exists: `docs/decisions.md` → "RLS policies never
reached prod".
