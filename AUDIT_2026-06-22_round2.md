# wyrdfold audit — round 2 (2026-06-22)

Follow-up to [#29](https://github.com/danieljoffe/wyrdfold/issues/29) covering the four areas the first pass left out: **frontend**, **supply chain / deps**, **CI/CD & infra**, and a **deep async/perf** sweep. Monorepo-wide, commit `be98601`, read-only.

Method: same cost-optimized pattern as the round-1 perf tail — 7 Opus finders → dedup → skip-info → **Haiku** per-file batched verifiers → one independent Haiku second-opinion on every high → Haiku coverage critic. **35 agents / ~1.37M tokens.**

**27 confirmed** (after adversarial verification): 1 critical, 6 high, 5 medium, 15 low. Plus 9 informational (incl. 4 positive controls) and 10 deeper-sweep follow-ups.

> Two top findings were additionally hand-verified against the live files (`railway.toml`, `Dockerfile`, `api/jobs/poll/route.ts`) before publishing — notes inline.

| Severity | Count           |
| -------- | --------------- |
| Critical | 1               |
| High     | 6               |
| Medium   | 5               |
| Low      | 15              |
| Info     | 9 (4 positives) |

---

## CRITICAL

### C1 — `railway.toml` startCommand needs `uv` (absent from the runtime image) and drops `--proxy-headers`

- **`apps/wyrdfold-api/railway.toml:6`** — `startCommand = "uv run --package wyrdfold-api uvicorn app.main:app …"`.
- **Hand-verified:** the Dockerfile copies `uv` only into the _builder_ stage (`Dockerfile:9`); the runtime stage (`FROM python:3.11-slim AS runtime`, line 32) has no `uv` — the venv's `uvicorn` is on PATH (line 53). So `uv run` resolves to `uv: not found` on the path where Railway honors `railway.toml`. The startCommand also omits `--proxy-headers --forwarded-allow-ips` that the Dockerfile `CMD` (line 68) sets.
- **Calibration:** prod is live (public launch 2026-06-20), so Railway must currently be falling back to the Dockerfile `CMD` or a dashboard override — i.e. the committed `railway.toml` startCommand is either **dead config or a latent deploy-breaker**. Either way it's wrong and drifted. When the railway.toml command _is_ used, dropping `--proxy-headers` means uvicorn ignores `X-Forwarded-For`, so `request.client.host` becomes the LB IP and the pre-auth slowapi rate-limit buckets (`rate_limit.py:43` `get_remote_address`) collapse all anonymous callers into one shared bucket.
- **Fix:** delete `[deploy].startCommand` (use the Dockerfile `CMD`), or rewrite it to call `uvicorn` directly (on PATH) **with** `--proxy-headers --forwarded-allow-ips`. Add a boot smoke-test. **Verify which start command prod actually runs.**

---

## HIGH

### H-r2-1 — `/api/jobs/poll` silently upgrades any user session to the admin cron API key → any user can trigger a global all-tenant poll

- **`apps/wyrdfold/src/app/api/jobs/poll/route.ts:21-47`** — **hand-verified.** If the request isn't the cron secret, it accepts **any authenticated session** (`accessToken !== null`, no admin check), then forwards to the upstream admin-gated `/poll` with the shared `WYRDFOLD_API_KEY` (`x-api-key`), **not** the user's JWT. So any logged-in user can invoke the cron-only global poll (force-polls every enabled source across all tenants, RLS-bypassed) — repeatable cost/abuse amplification.
- **Fix:** don't blanket-promote a session to the cron key. Restrict the session path to admins, or remove it and require the cron secret; for user-initiated polling, forward the user's JWT to a properly user-scoped backend endpoint.

### H-r2-2 — Event-loop blocking in ~20+ async handlers _(corroborates round-1 P-H1)_

- `routers/feedback.py` (create/remove/list_feedback, run_learner_now), `routers/targets.py:1313` (vote_on_reference_jd), etc. — independently re-found by the async-perf finder, with the added detail that uvicorn runs **single-process, no `--workers`** (Dockerfile:68 / railway.toml:6), so one blocking `.execute()` freezes the whole service. Same fix + CI guard as P-H1; this is the same issue, not a new one.

### H-r2-3 — `to_thread` DB calls share the default 40-thread anyio pool with no ceiling — poller fan-out can starve user requests

- `services/poller.py` (POLL_CONCURRENCY=10, LLM_CONCURRENCY=3; no limiter override anywhere). Background poll/rescore fan-out and FastAPI's sync-handler threadpool draw from the **same** 40-token pool, so a poll tick can head-of-line-block interactive request DB calls — latency spikes correlated with poll cadence.
- **Fix:** dedicated bounded executor / `anyio.CapacityLimiter` for background DB work, sized below the default pool; or raise the default limiter explicitly and size it deliberately.

### H-r2-4 — Bulk personal-data export & resume-zip buffer the entire archive in memory _(extends round-1 P-H2)_

- `services/data_export.py` (`build_export_zip` → `io.BytesIO` ZIP of all rows + every Storage object) and `routers/tailor.py:389-432`. Peak RAM = full export size per concurrent export on a small replica → OOM risk.
- **Fix:** stream the ZIP (`StreamingResponse` + zipstream / `SpooledTemporaryFile`); `gather` the per-file `to_thread` downloads.

### H-r2-5 — Sentry `enableLogs: true` with no log scrubber

- `apps/wyrdfold/src/instrumentation-client.ts:12` (+ `lib/sentry.config.ts` server). Replay is correctly hardened (`maskAllText`, `blockAllMedia`) and `beforeSend` exists, but the **Logs** feature captures `console`/logger args, which replay masking doesn't cover. Any future log line with resume text / prompts / tokens / email would ship to Sentry unredacted.
- **Fix:** add a `beforeSendLog` scrubber (and keep `beforeSend`), or disable `enableLogs` unless used; document "no PII to console/logger."

### H-r2-6 — `railway.toml restartPolicyMaxRetries = 3` can leave the service permanently down

- `railway.toml:9-10`. After 3 failed starts Railway stops retrying until a manual redeploy — and a crash-loop from C1 would burn them fast. Pair with alerting on the `/health` check, or raise the budget.

---

## MEDIUM

- **M-r2-1 — Docker base/tool images pinned by mutable tags, not digests** (`Dockerfile:7,9,32`; `uv:latest`). Non-reproducible builds + supply-chain surface. Pin `@sha256:…` and a concrete `uv` version.
- **M-r2-2 — No `.dockerignore`** → the whole monorepo root (incl. `.env`, `.env.local`, `.vercel`, `.git`) is sent to the (remote) build daemon. Add a root `.dockerignore`.
- **M-r2-3 — Long-running SSE/LLM/export handlers never check `request.is_disconnected()`** (`routers/experience.py:454-567`, exports). Abandoned requests keep burning CPU + **LLM tokens** (BYOK cost). Poll `is_disconnected()` between deltas and break.
- **M-r2-4 — SSE derive generator has no `try/finally` around the live LLM stream** (`experience.py:481-560`). Mid-stream provider error → truncated stream, no error frame, upstream closed only by GC. Wrap to emit a terminal `error` event + `aclose()`.
- **M-r2-5 — Trivy (the only Python-tree scanner) runs `--ignore-unfixed` + OSV/GitHub DB** (`ci.yml:311-322`), missing unfixed/PyPI-only advisories. Add `pip-audit`/OSV against the uv lock. (Currently moot — clean scan.)

## LOW (15)

_Container/infra:_ FORWARDED_ALLOW_IPS="*" image default (Dockerfile:66, documented for Railway) · Trivy scanner image tag-pinned **and** mounts host `docker.sock` (ci.yml:314-316) · pandoc/Supabase `.deb` installed via `dpkg -i` with no checksum (ci.yml:181-247) · image-scan builds untrusted **fork-PR** code with the socket mounted (correctly uses `pull_request`, not `pull_request_target`) · `scheduler.shutdown(wait=False)` abandons in-flight cron ticks (main.py:121) · no Starlette/uvicorn request-body-size limit (app-level checks only, after buffering).

_Frontend:_ CSP `script-src` keeps a broad `https:` fallback alongside `strict-dynamic` (proxy.ts:30) · sanitized job-desc HTML allows `target="_blank"` without `rel="noopener"` (JobDetailPanel.tsx — DOMPurify _does_ block the XSS) · `from-url`/`from-manual` BFF routes forward unvalidated input (no SSRF/scheme check at the boundary; defense-in-depth) · state-changing proxy routes rely solely on SameSite=Lax for CSRF (no Origin/Referer check).

_Data race / deps:_ contribution-vote suppression tally + `profile_version` bump is read-modify-write with no tx/row-lock → lost-update on concurrent votes (votes.py:62-96) · `ffmpeg-python==0.2.0` abandoned, transitive via `voyageai` (dead weight) · `pyproject.toml` direct deps are `>=`-only (mitigated by the frozen `uv.lock`) · `http-proxy-middleware@3.0.6` CVE-2026-55603 (**dev/build-only**, via `@nx/*`) · `piscina@4.9.2` CVE-2026-55388 (**dev/build-only**, via `@swc/cli`).

---

## Positive controls (verified-safe)

- ✅ **Python supply chain clean** — no known-vulnerable deps, all PyPI-sourced, frozen lock.
- ✅ **Frontend prod tree clean** — the JS CVEs are all dev/build-only; CI gates with `pnpm audit --prod` + Trivy.
- ✅ **GitHub Actions: `GITHUB_TOKEN` least-privilege + third-party actions SHA-pinned** — correctly done.
- ✅ **`evals.yml` `OPENROUTER_API_KEY` walled off from fork PRs** — no leak.
- ✅ **`asyncio.gather` `return_exceptions` is consistent** across ATS fetchers + poller — hypothesis from round 1 **denied**.

Also noted (info): security headers split between middleware/next.config with no CSP fallback if middleware is bypassed; CSP `style-src 'unsafe-inline'`; `/api/email/target-paused` forwards an unvalidated recipient to Resend behind a shared secret; `webpack-dev-server@5.2.4` CVE (dev-only).

## Deeper-sweep follow-ups (surfaced, not verified)

Coverage critic flagged these _classes_ for a future dedicated pass: systematic multi-tenant isolation test (user-A-accesses-tenant-B across all endpoints) · transitive-dep depth vs Trivy/OSV gaps · container resource limits / liveness-readiness probes · inter-service auth (API↔poller↔scheduler) · secrets lifecycle & rotation · async deadlock / lock-ordering under load · unbounded in-memory queues/caches (OOM) · DOMPurify config vs SVG/iframe/MathML payloads · error-message info disclosure (stack traces / SQL / internal URLs in responses).

---

_Generated by a cost-optimized multi-agent audit workflow (Claude Code), 2026-06-22, commit `be98601`. 35 agents / ~1.37M tokens. C1 and H-r2-1 hand-verified against source. AI-authored — re-confirm each item against current code before acting._

_— Claude (Claude Code)_
