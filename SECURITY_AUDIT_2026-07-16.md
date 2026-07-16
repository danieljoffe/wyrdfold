# Security Audit — wyrdfold-api + BFF (2026-07-16)

Scope: SQL + Python security. Method: two specialist finders (Python/API surface,
SQL/RLS surface) + an independent crown-jewel cross-check, **every finding below
verified against code** (file:line + reachability confirmed) before inclusion.

**Verdict:** A mature, heavily-audited codebase. The perimeter — SSRF defense,
BYOK crypto, JWT verification, secret handling, injection, uploads, GDPR flows —
held up under scrutiny. The real exposure is **one systemic blind spot**: the
`targets` router treats `targets` as a shared multi-tenant catalog but guards its
destructive/mutating routes with a **membership-only** check on the **service-role**
client (RLS bypassed), so one tenant can destroy or poison data belonging to every
co-follower of a shared target. Plus one missing-guard info-disclosure.

The shared model is **not hypothetical**: `find_matching_target`
(`services/targets/match.py:53`) dedups new targets by normalized label globally
(exact, then fuzzy pg_trgm ≥0.7, **no user scope**), and `from_input.py` links the
new user onto the **same** `targets` row. Any two users who type "Software Engineer"
automatically co-own one row — no UUID-guessing required. So the destructive
findings below fire on **normal use**, not only as a deliberate attack.

---

## HIGH

### SEC-1 — `DELETE /targets/{id}` hard-deletes the shared catalog row for all co-tenants

`routers/targets.py:1101` → sink `services/targets/crud.py:221`

`delete_target` calls `_require_user_owns_target` (confirms only that the caller is
_one of possibly many_ linked users — its own docstring: _"Targets are a shared
resource… there is no RLS backstop because the API uses the service-role key"_),
then `crud.delete`: an unscoped `targets.delete().eq("id", target_id)` on the
service-role client. All six FKs referencing `targets(id)` are `ON DELETE CASCADE`
(`user_targets`, `job_target_scores`, `job_feedback`, `job_analyses`,
`target_learning_log`, `target_reference_jds` — migration
`20260612015641_remote_schema.sql:1461,1471,1511,1536,1541,1551`).

- **Failure scenario:** User B deletes their "Software Engineer" target (organically
  shared with User A) → the shared row and **every co-follower's** links, match
  scores, feedback, LLM analyses, and learning history are irreversibly wiped. User
  A's targets silently vanish. `account_deletion.py`'s own docstring states the
  shared catalog must **never** be deleted on erasure for exactly this reason —
  `delete_target` does what erasure forbids.
- **Fix:** For JWT callers, call the existing `crud.unlink_user_from_target(user_id,
target_id)` (`crud.py:503`) — it removes only the caller's link, and a DB trigger
  deactivates the target when no active users remain. Hard-delete the catalog row
  only on the operator path (`user_id is None`) or when no `user_targets` remain.
  Mirrors the audit-#29 H1 jobs fix (user delete → per-user soft-archive).

### SEC-2 — `PATCH /targets/{id}` lets any follower overwrite the shared scoring profile

`routers/targets.py:577` → sink `crud.py:185`

Membership-only guard, then an arbitrary `TargetUpdate` body (`scoring_profile`,
`search_keywords`, `label`, `is_active`, `profile_version`) is written straight onto
the shared row via service-role `crud.update` — no cap, no version guard, no
content-ownership check.

- **Failure scenario:** A follower `PATCH`es an attacker-chosen `scoring_profile`
  (or empty keywords / match-everything negatives) onto a target followed by N
  users; every follower's job scoring for that target is now driven by the
  attacker's JSON. This end-runs the entire #191 contribution machinery
  (`apply_profile_merge_rpc` + per-contributor cap + quarantine) that `add_reference_jd`
  uses specifically to bound one contributor's influence.
- **Fix:** Don't accept shared-catalog fields (`scoring_profile`/`search_keywords`/
  `label`) from a co-follower on this route — restrict to a sole-owner target, or
  route changes through the capped merge RPC.

---

## MEDIUM

### SEC-3 — `POST /targets/{id}/derive-profile` re-derives + overwrites the shared profile

`routers/targets.py:958`. Same root cause as SEC-2 (membership-only guard → shared
`crud.update`), but the resulting profile is LLM-derived-from-label (less
attacker-controlled), so MEDIUM. Discards the collectively-merged profile for all
co-followers and spends an LLM call. Fix: same as SEC-2.

### SEC-4 — `POST /targets/{id}/deactivate` is missing its ownership guard (info disclosure)

`routers/targets.py:682`. Unlike every sibling route, `deactivate_target` has **no**
`_require_user_owns_target`. It calls unscoped `crud.get(target_id)` on the
service-role client and returns the full `JobTarget` (label, `scoring_profile`,
`search_keywords`, `example_*_titles`, `description`). The `set_user_target_inactive`
between is scoped to the caller's own link (harmless no-op for a non-owner), but the
response still leaks the row.

- **Failure scenario:** Any JWT user `POST`s `/targets/{any_id}/deactivate` and
  receives another tenant's full target metadata — the exact enumeration/disclosure
  that `get_target` and `get_target_status` explicitly guard against (citing audit
  #29 R3 M2-M3). This route was missed.
- **Fix:** Add `_require_user_owns_target(supabase, user_id=user_id,
target_id=target_id)` before the read. One line.

### SEC-5 — spoofable IP rate-limit on public endpoints

`Dockerfile:66/68` (`FORWARDED_ALLOW_IPS="*"` + uvicorn `--proxy-headers
--forwarded-allow-ips=*`) → `request.client.host` is taken from a caller-supplied
`X-Forwarded-For`. Public endpoints (`waitlist.py:65` 5/min;20/hr, `:108`
signup-mode 30/min) key on `ip:<host>` (`rate_limit.py _user_or_ip_key`). A direct
hit to `*.up.railway.app` rotating `X-Forwarded-For` mints a fresh bucket per
request → bypass. JWT/LLM/cost-bearing endpoints key on `jwt:<sub>` and are
unaffected; impact is bounded to waitlist-stuffing / signup-mode flood (no data
exposure, no LLM cost). The Dockerfile comment already flags this as a deliberate
tradeoff (#30 F4).

- **Fix (infra):** network-allowlist the API to the BFF/LB, then pin
  `FORWARDED_ALLOW_IPS` to those IPs; or enforce the public-endpoint limits at the
  Vercel edge where the client IP is trustworthy. Not a code fix.

---

## LOW / hardening

- `services/tailor/versions.py` (`checkpoint`/`list_for_resume`/`record`/`_prune`) is
  user-unscoped — safe today (RLS client + `persistence.get(..., user_id=...)` gate),
  but a future service-role caller with a client-supplied `resume_id` turns it into a
  cross-tenant read/destructive-write. Thread `user_id` through.
- `models/experience.py PreferencesPayload.{rules,avoid,tone_notes}` — unbounded
  `list[str]` injected into tailor LLM prompts (own-budget blast radius). Add caps
  (peers cap 20k–500k).
- `routers/targets.py:292 create_target` — any JWT user creates unlimited bare
  target rows (no rate limit). Spam/clutter of the shared catalog; low.
- BFF `src/app/api/targets/[id]/route.ts` (+ ~40 siblings) interpolate path params
  into upstream paths without `encodeURIComponent` (unlike `profile/keys/[provider]`).
  No privilege gain (carries only the caller's JWT); hardening.
- `jobs.py list_jobs` has an unreachable operator branch with stale comments claiming
  a service-role fallback that no longer exists — delete so a future maintainer
  doesn't "fix" it into reachability.

---

## Surfaces reviewed and found SAFE (with evidence)

- **SSRF** — `safe_http.py _PinningBackend` resolves the host, rejects if _any_
  resolved IP is disallowed (split-horizon defense), pins the validated IP at connect
  (defeats DNS-rebind), re-validates every redirect hop; `_is_disallowed_address`
  covers loopback/link-local (incl. 169.254.169.254)/RFC1918/ULA/IPv4-mapped/
  reserved/multicast. Both user-URL fetch paths (`jobs.py:1955` manual job add,
  `targets.py:1264` from-url) route through `assert_safe_host` + the pinning transport
  with a redirect cap and http(s)-only scheme enforcement. ATS probers host-locked by
  fixed base URLs + strict slug regex. Firecrawl offloads the fetch to a fixed
  third-party host.
- **BYOK key crypto** — `keys/crypto.py` AES-256-GCM envelope, fresh 96-bit nonce,
  auth-tag integrity; `store.py` scopes every op by JWT `user_id`; ciphertext never
  returned (provider+last4 only) or logged.
- **JWT/auth** — JWKS verification with `aud`/`iss`/`exp`/`sub` required, algorithms
  pinned; operator keys via `hmac.compare_digest`; cron key deliberately excluded
  from the user-data gate; failures collapse to 401.
- **Secrets/errors** — every secret setting `repr=False`; generic 500 bodies
  (`debug_errors` fail-closed); no vendor text/keys/PII in responses; CORS is an
  explicit allowlist with `allow_credentials=False`.
- **Injection** — no raw SQL; the two PostgREST `.or_()` interpolations are safe
  (`min_score` validated int; search tokens through `_escape_or_token` stripping
  `,`/`(`/`)`); pandoc via `subprocess.run` list-args + stdin (no shell).
- **IDOR across other routers** — `jobs.py`, `feedback.py`, `analysis.py`,
  `tailor.py`, `status.py`, `billing.py` (signature-verified webhook),
  `insights.py`/`user_profile.py`/`experience.py`, `keys.py` all ownership-gated or
  RLS-user-client scoped. The targets.py findings are the exception because those
  routes use the service-role client.
- **Uploads/GDPR** — resume upload capped 10 MB + parse timeout + 3/min, stored under
  `{user_id}/`; export/deletion scoped to JWT `user_id`, key ciphertext redacted.
- **Deps** — CI Snyk + Trivy gates green on the last release.

---

## Actionables (priority order)

| ID    | Sev  | Action                                                                           | Self-implementable |
| ----- | ---- | -------------------------------------------------------------------------------- | ------------------ |
| SEC-1 | HIGH | `delete_target` JWT path → `unlink_user_from_target`                             | Yes                |
| SEC-2 | HIGH | `update_target` — reject shared-catalog fields from co-followers                 | Yes                |
| SEC-4 | MED  | `deactivate_target` — add `_require_user_owns_target`                            | Yes (one line)     |
| SEC-3 | MED  | `derive-profile` — gate shared-profile overwrite                                 | Yes                |
| SEC-5 | MED  | Rate-limit XFF — network allowlist / edge enforcement                            | No (infra)         |
| LOW×5 | LOW  | versions user_id, Preferences caps, create_target limit, BFF encode, dead branch | Yes (batch)        |

---

# SQL / RLS / Migration layer

**Bottom line: 0 HIGH — multi-tenant isolation is sound and CI-enforced.** Every
table has RLS enabled; all 20 `SECURITY DEFINER` functions pin `search_path`; the
DEFINER writers on shared tables re-check ownership; a live `test_privilege_invariants.py`
integration test pins anti-drift invariants in CI. Reachability confirmed real (the
anon key ships to the browser, PostgREST is internet-reachable) — so RLS + GRANTs are
the genuine control, and they hold. This independently confirms the `targets` findings
above are an **app-layer** problem (those routes use the service-role client, which
bypasses RLS) — not a DB-RLS gap.

## SEC-6 — MEDIUM: broad default privileges make future tables born-permissive

`migrations/20260612015641_remote_schema.sql:2578-2598` — `ALTER DEFAULT PRIVILEGES
FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES/FUNCTIONS/SEQUENCES TO anon,
authenticated`. Every **future** `postgres`-created object in `public` is born with
full anon+authenticated privileges; the whole perimeter then rests on RLS-deny plus an
ad-hoc per-object `REVOKE` in each migration (already cleaned up 4× for
`notifications_sent`/`user_api_keys`/`job_embeddings`/`prescan_shadow`). Contained today
because every `CREATE TABLE` migration hand-enables RLS, **but** the invariant test pins
only 2 named tables for grant-drift — a 3rd new internal table shipped tomorrow with a
broad grant + a careless `USING(true)` would not be caught.

- **Fix (self-implementable):** generalize the privilege-invariant test from a 2-table
  allowlist to "every public table is either in an explicit owner-scoped-RLS allowlist
  or has no anon/authenticated grant," so new internal tables **fail closed** in CI.
  Highest-value DB action. (Longer term: flip the default privileges off — larger
  posture change.)

## LOW (DB hygiene)

- **SEC-7** `user_apply_score_blend_rpc.sql:49` (+ verbatim in `20260702120000:157`) —
  the DEFINER RPC's `UPDATE jobs SET llm_analysis_id … WHERE id = p_job_posting_id` is
  not bound to the owned target (gate only checks the caller follows `p_target_id`). A
  follower of _any_ target can repoint a shared job's `llm_analysis_id` / set a shared
  `(job,target)` score. Shared-catalog integrity, **not** a cross-tenant read (analyses
  stay RLS-scoped). Fix: add `AND EXISTS (SELECT 1 FROM scores WHERE
job_posting_id=p_job_posting_id AND target_id=p_target_id)` to the authz check.
- **SEC-8** `rls_auto_enable()` event-trigger _function_ exists
  (`20260612015641:316`) but **no `CREATE EVENT TRIGGER` binding it** is in any tracked
  migration (verified) — the "new tables born RLS-on" backstop is untracked prod infra
  or dead; CI can't reproduce it. Fix: add the `CREATE EVENT TRIGGER … EXECUTE FUNCTION
rls_auto_enable()` to a migration, or delete the dead function + its comments.
- **SEC-9** `GRANT ALL ON targets TO anon, authenticated` (`20260612015641:2345`)
  persists while `targets` RLS is SELECT-only `USING(true)` — the write half is inert
  (RLS denies) but broader than intent. Same for `anon` EXECUTE on the authenticated-
  only read RPCs (`get_target_jobs`, `pipeline_counts`). Fix: `REVOKE INSERT,UPDATE,
DELETE ON targets FROM anon, authenticated` + drop the inert `anon` grants. Hygiene.

## DB verified-safe (spot-checked)

Per-user tables owner-scoped with `WITH CHECK` (no user_id reassignment); sensitive
tables deny-all for anon/authenticated (`user_api_keys`, `waitlist_signups`, `sources`,
`app_settings`, `job_embeddings`, …); DEFINER writers on shared tables re-enforce
ownership + version guards; `get_target_jobs` dynamic SQL uses whitelisted columns +
parameterized `USING` (no interpolation); spend/financial RPCs carry a triple-layer
`auth.uid()` self-guard; the one historical `USING(true)` loosening (`reference_jds`)
was caught + tightened. All 72 migrations forward-only + idempotent. The one destructive
DDL (`DROP COLUMN jobs.status`) was a guarded, sequenced cutover.

---

# Consolidated actionables (priority order)

| ID    | Sev  | Layer | Action                                                                           | Self-impl       |
| ----- | ---- | ----- | -------------------------------------------------------------------------------- | --------------- |
| SEC-1 | HIGH | Py    | `delete_target` JWT path → `unlink_user_from_target`                             | Yes             |
| SEC-2 | HIGH | Py    | `update_target` — reject shared-catalog fields from co-followers                 | Yes             |
| SEC-4 | MED  | Py    | `deactivate_target` — add `_require_user_owns_target` (1 line)                   | Yes             |
| SEC-3 | MED  | Py    | `derive-profile` — gate shared-profile overwrite                                 | Yes             |
| SEC-6 | MED  | SQL   | generalize privilege-invariant test to fail-closed                               | Yes             |
| SEC-5 | MED  | Infra | rate-limit XFF — network allowlist / edge enforcement                            | No              |
| SEC-7 | LOW  | SQL   | bind score-blend RPC's jobs write to the owned target                            | Yes (migration) |
| SEC-8 | LOW  | SQL   | track or delete the `rls_auto_enable` event trigger                              | Yes (migration) |
| SEC-9 | LOW  | SQL   | revoke inert anon/authenticated write grants                                     | Yes (migration) |
| LOW×5 | LOW  | Py/TS | versions user_id, Preferences caps, create_target limit, BFF encode, dead branch | Yes (batch)     |

**Overall verdict:** heavily-audited, mature codebase; **the one urgent item is the
shared-`targets` app-layer class (SEC-1/2)** — a JWT tenant destroys or poisons every
co-follower's data through the service-role client behind a membership-only check. The
DB layer itself has no reachable isolation hole. GitHub issue #366 tracks the app-layer
class; SEC-6 (CI hardening) and the LOW hygiene items are documented here.
