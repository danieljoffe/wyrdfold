# wyrdfold audit — round 3 (deeper sweep, 2026-06-22)

Follow-up to [#29](https://github.com/danieljoffe/wyrdfold/issues/29), covering the deeper-sweep classes that rounds 1–2 surfaced but didn't verify: **multi-tenant isolation matrix, secrets/inter-service auth, error/info disclosure, HTML-sanitization bypass, resource-exhaustion/queues/deadlock, transitive-dep depth.** Finders were told to skip everything already in #29 / fixed in #31/#32, so these are **new or materially-deeper** issues.

Method: same cost-optimized pattern (6 Opus finders → dedup → skip-info → Haiku per-file batched verifiers → 2nd-opinion on highs → Haiku critic). **31 agents / ~1.45M tokens.** **19 confirmed** (the two `DELETE /jobs` entries are the same issue found by two finders → **18 distinct**): 7 high, 8 medium, 3 low. Plus 8 informational incl. **5 positive controls**.

> **Secrets are redacted in this report.** Two findings concern live credentials in local (gitignored) `.env.local` files — no secret values are reproduced here.

| Severity | Count (distinct) |
|---|---|
| High | 7 |
| Medium | 8 |
| Low | 3 |
| Info | 8 (5 positives) |

## ⚠️ Two items needing action beyond a code PR

1. **`DELETE /jobs/{id}` cross-tenant data destruction (H1)** — *live now*, triggered by the ordinary "delete job" button. Highest-priority functional security bug.
2. **Rotate the production Supabase service-role key (H3)** — operational action only you can do (Supabase dashboard → Settings → API → reset `service_role`). It's a ~10-year, unrotated, full-RLS-bypass key reused across dev *and* prod.

---

## HIGH

### H1 — `DELETE /jobs/{posting_id}` destroys a SHARED catalog row, cascade-wiping every other user's pipeline *(escalates round-1 M1)*
- **`apps/wyrdfold-api/app/routers/jobs.py:1749-1766`** (BFF `apps/wyrdfold/src/app/api/jobs/[id]/route.ts`). `jobs` is a shared, deduplicated catalog with **no owner column**; per-user state lives in `user_jobs`/`scores`. `_assert_user_owns_posting` only checks the caller follows *some* target with a `scores` row for the posting — true for many users on any poller-ingested popular posting. It then runs an **unscoped service-role** `jobs.delete().eq('id', …)`, and FK `ON DELETE CASCADE` wipes `scores`, `job_feedback`, `job_status_log`, `user_jobs`, etc. for **every** user.
- Found independently by **two** finders. Round 1 flagged this at *medium* (M1); the cascade-FK cross-tenant-destruction detail escalates it to **high** — and it fires on normal UI usage ("Job deleted" toast), not just adversarially.
- **Fix:** make "delete" a per-user soft action (set `user_jobs.status='archived'` / remove only the caller's own `scores`), never touching the shared `jobs` row; or gate a true global delete behind operator/api-key auth. Mirror the existing per-user scoping (`user_set_scores_included`, status routes).

### H3 — Long-lived (~2036), unrotated production service-role key reused across dev and prod
- **`apps/wyrdfold-api/.env.local`** (gitignored — **not** a git-history leak; confirmed untracked). A `service_role` JWT (full RLS bypass; iat 2026-06-11, exp ≈ 2036) plus the Postgres pooler password live in local env files, and the **same prod key + project ref** are wired into the dev environment (dev Railway URL, same Supabase project).
- **Impact:** one key compromise (dev box / CI / dev Railway / prod) = full multi-tenant breach (all resumes, JDs, profiles, BYOK ciphertext) until 2036, with no rotation path.
- **Fix:** use a **separate Supabase project** for dev/staging; **rotate the prod `service_role` key now**; adopt a rotation cadence and shorter-lived credentials. *(Also info: a stale Vercel OIDC token sits in repo-root `.env.local` — clean it up.)*

### H4 — Single shared `WYRDFOLD_API_KEY` is accepted as full auth on six user-data routers (over-broad scope)
- **`apps/wyrdfold-api/app/dependencies.py:250-276`** via `verify_api_key_or_jwt` on `jobs`, `targets`, `sources`, `analysis`, `tailor`, `experience`. `.env.example` documents it as cron/poller-only, but a leak of this one key authenticates against user data + drives operator-billed LLM spend, with no per-capability scoping.
- **Fix:** split into a cron/automation key (only the `verify_api_key` cron routers) and remove api-key acceptance from user-facing routers (require JWT, or a distinct scoped principal).

### H5 — Production verbose-error gate is **fail-open**
- **`apps/wyrdfold-api/app/main.py:280-285`** + **`config.py:46`**. The 500 handler returns raw `f"{type(exc).__name__}: {exc}"` + request path unless `settings.sentry_environment == "production"` — but that defaults to `"development"` and is **not set in any deploy config** (`railway.toml` has no override). So prod can leak exception types / SQL / PostgREST / file paths to any caller who triggers a 500.
- **Fix:** make the gate fail-closed (default to prod-safe unless an explicit non-prod/DEBUG flag is set), or pin `SENTRY_ENVIRONMENT=production` in `railway.toml`.

### H6 — `cost_log_buffer` grows unbounded during a Supabase write outage → OOM
- **`apps/wyrdfold-api/app/services/llm/cost_log_buffer.py:73-124`**. `max_size=100` only gates the early-flush wakeup — there's no cap/drop/overflow. On flush failure, `_requeue` pushes rows back and the loop swallows the error, so a sustained Supabase write outage grows `_rows` until the process OOMs (and rows are lost on restart anyway).
- **Fix:** hard ceiling on `_rows` (drop-oldest/refuse past N); chunk the bulk INSERT to a bounded batch.

### H7 — Liveness-only `/health` + no container CPU/memory limits → wedged/leaking process stays "healthy" and OOMs the host
- **`railway.toml:8`** + **`main.py:304-306`** (static `{"status":"ok"}`, touches nothing) + no memory/CPU limits in `railway.toml`/`Dockerfile`. DB-down outages serve errors behind a green probe; leaks (see H6) crash the box instead of being bounded/restarted.
- **Fix:** add a readiness probe that checks a critical dependency (cheap Supabase ping / pool state) and point the LB at it; declare explicit memory/CPU limits.

### H8 — SSRF validator echoes the resolved internal IP/host back to the caller
- **`apps/wyrdfold-api/app/services/validate.py:128,134`**, reflected at `targets.py:1103/1120/1132` and `jobs.py:1309/1330/1348` via `detail=str(exc)`. The SSRF block itself holds, but the rejection messages let an authenticated user enumerate which internal hostnames resolve / map to private ranges — an internal-recon oracle.
- **Fix:** return a generic "This URL cannot be fetched"; keep resolved-IP/host detail in server logs only.

---

## MEDIUM

- **M1 — No application-level CSP on document/HTML responses** (`apps/wyrdfold/next.config.mjs:62-134` sets HSTS/XFO/etc. but no `Content-Security-Policy`; only the next/image block has one). A single DOMPurify regression then executes unconstrained. Add a strict document CSP via `headers()`/middleware.
- **M2 — `GET /targets/{id}/status` has no ownership check** (`targets.py:898-927`): any authenticated user reads any target's activation status + job count. Add `_require_user_owns_target`, 404 for non-owners.
- **M3 — `GET /targets/{id}` and `GET /targets/{id}/reference-jds` have no ownership check** (`targets.py:488-496, 1253-1259`) and read via service-role — exposes any target's full JD text **and contributor `user_id`s** (deanonymizes the "anonymous" contribution graph). *Extends round-1 L4/L6 with the per-id API routes.* Add `_require_user_owns_target`; strip `user_id` from the response.
- **M4 — Tailored-resume download leaks raw Storage/pandoc exception text** (`tailor.py:686,701,732`) — interpolated `detail=f"…{exc}"` bypasses the prod error gate. Use a generic message + server-side log.
- **M5 — JD render DOMPurify `html` profile allows CSS injection, `<img>` beacons, DOM clobbering, in-app `<form>` phishing** (`JobDetailPanel.tsx:299-305,558`) from attacker-influenced job-posting bodies. No script exec, but stored-content injection into an authed session. Tighten to an explicit allow-list matching the server's bleach config; `FORBID_ATTR:['style']`, forbid `form`/`svg`/`style`.
- **M6 — Shared httpx pool (`max_connections=20`) is oversubscribed by the poller's 10×5 fan-out** (`http_client.py:29-37`; `poller.py:89` × `smartrecruiters.py:15`/`workday.py:14`) → ~50 concurrent requests contend, detail fetches queue/timeout and postings drop. Size `max_connections` to `POLL_CONCURRENCY × max(_DETAIL_CONCURRENCY)` + headroom, or set an explicit pool timeout.
- **M7 — Reference-JD downvote suppression is Sybil/griefing-exploitable** (`votes.py:65-99`; quorum=3 default + open `POST /targets/{id}/link`). The anti-poisoning mechanism becomes a cheap censorship lever — a few self-linked accounts suppress any contribution. Scale quorum to follower count; gate vote eligibility (account age/own-contribution); cap votes per user.
- **M8 — Target-activation retro-score loads all matching job ids into one unbounded in-memory list** (`target_scoring.py:249-265`) — scales with catalog size. Process page-by-page (as `_retro_score_existing_jobs` already does).

## LOW

- **L1 — textarea entity double-decode runs before DOMPurify** (`JobDetailPanel.tsx:301-305`) — undoes the server's escaping client-side, collapsing to the single permissive DOMPurify pass. Decode+sanitize once, strictly; prefer fixing encoding server-side.
- **L2 — Lazy per-process user-httpx singleton has an init race + no `limits=`** (`supabase_pool.py:42-46`) — runs across threadpool threads; double-construct leaks a client. Init eagerly in `init_supabase()` or guard with a lock; set `httpx.Limits`.
- **L3 — Trivy is the sole Python vuln gate, capped at HIGH/CRITICAL + `--ignore-unfixed`** (`ci.yml:310-321`) — misses MEDIUM/unfixed Python advisories. Add a `pip-audit`/`osv-scanner` step against `uv.lock` at a MEDIUM+ floor.

---

## Positive controls (verified-safe)
- ✅ **Auth/isolation matrix is otherwise consistent** — identity is always derived from the verified JWT, never from request params (H1–H4 / M2–M3 are the specific exceptions).
- ✅ **BYOK key crypto + storage + the per-user/service-role client split are sound; no secret leakage.**
- ✅ **Error gating is mostly correct** — BFF proxy, global LLM handler, auth layer, keys router, and Sentry scrubbing sanitize error detail (H5/M4 are the gaps).
- ✅ **TipTap `MarkdownPreviewEditor` (resume/cover-letter render) is a clean, non-bypassable path.**
- ✅ **Python transitive tree is clean per OSV** — 0 known vulns across all 122 resolved packages.

## Coverage-critic follow-ups
Mostly point back at the confirmed findings above. Net-new threads worth a future look: request/response size limits at the edge, DNS-rebinding TOCTOU (already known, still unresolved), and per-request DB connection limits.

---

*Generated by a cost-optimized multi-agent audit workflow (Claude Code), 2026-06-22. 31 agents / ~1.45M tokens. Secrets redacted. AI-authored — re-confirm each item against current code before acting; rotate the H3 key regardless.*

*— Claude (Claude Code)*

