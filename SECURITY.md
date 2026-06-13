# Security Policy

## Reporting a vulnerability

**Please do not open public issues for security findings.**

Report vulnerabilities through GitHub's [Private Vulnerability
Reporting](https://github.com/danieljoffe/wyrdfold/security/advisories/new)
form. That channel:

- keeps the details private while we work on a fix,
- gives you a CVE if the issue warrants one, and
- lets us credit you in the advisory if you'd like.

If you can't use GitHub for any reason, email
[joffe.daniel.90@gmail.com](mailto:joffe.daniel.90@gmail.com) and put
`[wyrdfold security]` in the subject. PGP isn't set up; please don't
include exploit details in the first email beyond what's needed to
establish a private channel.

## What's in scope

This repository — the FastAPI service in `apps/wyrdfold-api/` and the
Next.js app in `apps/wyrdfold/` — plus the Supabase schema in
`supabase/migrations/`.

Findings I'm especially interested in:

- **Tenant isolation** in the API. Every boundary lives in Python; the
  service-role key bypasses RLS. See
  [#24](https://github.com/danieljoffe/wyrdfold/issues/24) for the
  recent audit + landed fixes — and please poke at anything that audit
  missed.
- **Auth flow** between the Next.js BFF and the Python API (JWT
  forwarding, the shared `x-api-key` cron path).
- **SSRF / URL-fetcher abuse** in the manual job-add and reference-JD
  flows.
- **PII handling** — resume prose, generated DOCX artifacts, LLM logs
  (see [#29](https://github.com/danieljoffe/wyrdfold/issues/29) for the
  open privacy audit).

Out of scope: vulnerabilities in third-party dependencies that don't
have a working PoC against WyrdFold itself (dependabot handles
advisories), denial-of-service against self-hosted instances (the
operator owns their infra), and missing security headers on the marketing
shell that don't carry user data.

## Response expectations

This is a side project with no SLA. I'll acknowledge reports within a
few days and aim to ship a fix or mitigation plan within two weeks for
anything credible. If a finding is critical and a self-hoster can be hit
in the wild, I'll publish an advisory before the long-tail fix lands.

## Supported versions

Only `main` is supported. Self-hosters should track tagged releases
([Releases](https://github.com/danieljoffe/wyrdfold/releases)) and
update promptly when an advisory ships.
