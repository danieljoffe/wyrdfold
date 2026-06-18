# wyrdfold-api

FastAPI service that powers WyrdFold: Supabase persistence, LLM calls (Anthropic / OpenRouter), source discovery (Brave Search), and ATS polling (Greenhouse / Lever / Ashby). User requests authenticate with a Supabase Bearer token (validated against the project JWKS); `WYRDFOLD_API_KEY` is a shared secret reserved for cron / poller / batch callers.

## Local

```bash
uv run --package wyrdfold-api uvicorn app.main:app --reload --port 8001
pnpm nx test wyrdfold-api
```

Copy `.env.example` → `.env` and fill in Supabase + secrets. Default dev port is `8001`.

## Deploy to Railway

The service builds from the monorepo root so the uv workspace lockfile is in scope.

1. **Create the service**
   - New Project → Deploy from GitHub repo → select your WyrdFold repo.
   - In service **Settings**:
     - **Root Directory**: leave empty (repo root) — required so the Dockerfile can see `pyproject.toml` and `uv.lock`.
     - **Config Path**: `apps/wyrdfold-api/railway.toml`.
     - **Watch Paths**: `apps/wyrdfold-api/**` (prevents rebuilds when only the Next.js app changes).
     - **Networking → Target Port**: `8001`. Must match the port the container binds to. A mismatch returns `502 "Application failed to respond"` with `x-railway-fallback: true` even though the container is healthy.

2. **Set environment variables** (Settings → Variables) from `apps/wyrdfold-api/.env.example`. `SUPABASE_URL` is enough for JWT verification — the API fetches the project's public keys from `<SUPABASE_URL>/auth/v1/.well-known/jwks.json` (asymmetric ES256/RS256). No shared JWT secret to leak or rotate. `WYRDFOLD_API_KEY` is the cron-only shared secret (poller, batch). Set `SENTRY_DSN` (and optionally `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`) to enable error reporting.

3. **Generate the public domain** (Settings → Networking → Generate Domain). Copy the hostname.

4. **Wire Vercel** — set the WyrdFold app's API URL/key env vars to the Railway domain + matching key.

5. **Smoke-test**
   ```bash
   curl https://<railway-domain>/health
   # → {"status":"ok"}
   ```

## Evaluation

`scripts/eval_*.py` are offline LLM-quality harnesses (scoring, cover letters,
target suggestion, etc.). They read an **eval set you provide** at
`tests/fixtures/eval_set.json` — a snapshot captured from a live instance. It's
gitignored on purpose: it contains real user/job data, so it's never committed.
Bring your own. The shape is:

```jsonc
{
  "version": 1, "seed": 0, "captured_at_unix": 0,
  "targets": { "<target_id>": { "label", "payload" /* OptimizedPayload */,
                                 "profile_version", "target" /* scoring_profile */ } },
  "cases": [ { "target_id", "job_posting_id", "title", "jd_text",
               "baseline_score", "baseline_axes", "baseline_reasoning", "band" } ]
}
```

Run outputs land in `scripts/eval_results/` (also gitignored).

## Notes

- The Dockerfile uses `ghcr.io/astral-sh/uv:latest` for deps, then `python:3.11-slim` at runtime.
- Railway supplies `$PORT` at runtime; the `startCommand` in `railway.toml` binds to it.
- Healthcheck path is `/health`; Railway marks the deploy live once it returns 200.
- Auth: Supabase JWT for user requests, `WYRDFOLD_API_KEY` for cron / poller / batch. Persistence is scoped per authenticated `user_id` in the service layer; database-level tenant isolation via RLS is tracked in #79.
