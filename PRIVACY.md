# Privacy & Data Handling

WyrdFold stores the most sensitive data a job seeker has: full resume
prose, employment history, contact details, and per-job feedback. This
document is the operator-facing map of **what the app stores, where it
stores it, and what leaves your instance** — written for self-hosters,
who are the data controller for their own deployment.

It describes the current state of `main`. Where a capability is missing
(notably one-click account deletion and full data export), this document
says so plainly and links the tracking issue rather than implying a
guarantee that doesn't exist yet. See
[#29](https://github.com/danieljoffe/wyrdfold/issues/29) for the open
privacy audit this map belongs to.

> **Single-user vs. multi-tenant.** Today WyrdFold is designed for a
> single self-hosted operator who is also the only data subject. The
> obligations below (deletion, export, third-party disclosure) become
> real the moment a second person's data lives in the instance — which
> is the hosted/BYOK direction tracked in
> [#5](https://github.com/danieljoffe/wyrdfold/issues/5) and
> [#80](https://github.com/danieljoffe/wyrdfold/issues/80).

## What WyrdFold stores

All persistent data lives in your **Supabase Postgres** database and two
**Supabase Storage** buckets. Nothing is stored on the API container's
local disk beyond ephemeral request scope.

### Personal & sensitive data (per-user)

| Category                        | Where                                                        | Notes                                                                            |
| ------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| Identity & contact              | `user_profiles`                                              | email, name, phone number, location, LinkedIn/website URLs                       |
| Master experience prose         | `experience_prose_docs`                                      | freeform employment-history prose the user writes/dictates                       |
| Optimized experience doc        | `experience_optimized_docs`                                  | structured JSON derived from the prose (roles, skills, outcomes)                 |
| Experience embeddings           | `experience_chunks`                                          | vector embeddings of experience snippets (derived from employment history)       |
| Onboarding/update conversation  | `experience_conversation_turns`                              | full chat history; may contain anything the user typed about their career        |
| Experience preferences          | `experience_preferences`                                     | user preferences shaping resume/experience output                                |
| Uploaded resumes                | `uploaded_resumes` + `resume-uploads` bucket                 | extracted text **and** the original PDF/DOCX, keyed `{user_id}/…`                |
| Generated documents             | `documents`, `document_versions` + `tailored-resumes` bucket | tailored resume / cover-letter payloads, a JD snapshot, and the rendered `.docx` |
| Per-job feedback                | `job_feedback`                                               | the user's free-text reasons for liking/dismissing a job                         |
| Per-user keys (if BYOK enabled) | `user_api_keys`                                              | encrypted at rest; see [#5](https://github.com/danieljoffe/wyrdfold/issues/5)    |

### Operational data that can reference a person

| Category              | Where                 | Notes                                                                         |
| --------------------- | --------------------- | ----------------------------------------------------------------------------- |
| LLM cost ledger       | `llm_costs`           | per-call tokens/cost/purpose + a `metadata` JSON; **append-only, not purged** |
| Job analyses & scores | `analyses`, `scores`  | LLM reasoning that may quote the user's profile against a job                 |
| Target learning log   | `target_learning_log` | how a target's profile evolved from feedback                                  |
| Notification log      | `notifications_sent`  | which job alerts went to which profile/channel; **append-only**               |

### Shared catalog (not personal, by design)

`jobs`, `targets`, `sources`, `reference_jds`, and related tables hold
crawled job postings and target definitions that are intentionally shared
across users on a multi-tenant instance. They are not personal data,
though a user's _decisions_ about them live in the per-user tables above.

## What leaves your instance

WyrdFold is self-contained except for these outbound integrations. Each
is operator-configured; an integration with no credentials set is simply
inert.

- **OpenRouter (LLM provider)** — the heaviest disclosure. Resume prose,
  optimized experience docs, conversation turns, and job descriptions are
  sent on every analysis / tailor / fit / conversation call.
  **[Zero Data Retention](https://openrouter.ai/docs) is enabled
  account-wide in the OpenRouter dashboard, not enforced by this code** —
  the operator is responsible for keeping ZDR on and rotating the key.
- **Sentry (error tracking, optional)** — initialized with
  `send_default_pii=False`, so request bodies and user identifiers are
  not attached by default. There is **no `before_send` scrubbing hook**,
  so an exception that happens to carry user data in a local variable
  could still surface. Disabled entirely when no DSN is set.
- **Resend (email alerts, via the Next.js BFF, optional)** — receives the
  recipient email plus the alerting job's title, company, location, score,
  and URL. No resume content is sent.
- **Twilio (SMS alerts, optional)** — receives the recipient phone number
  and a short job-alert message. No resume content is sent.

## Retention & lifecycle

- **There is no automatic data purge.** The idle-account lifecycle
  (`idle_deactivate_days`, default 30) only flips a user's targets to
  inactive and sends one "paused" email — **deactivation is not
  deletion**. All prose, resumes, documents, feedback, scores, and logs
  remain until something explicitly deletes them.
- `llm_costs` and `notifications_sent` are append-only and grow
  unbounded; no retention window is applied today.

## Deleting & exporting data

Current capabilities are **partial and scoped** — there is no single
"delete my account" or "export everything" action yet.

What exists today:

- **Export:** `POST /tailor/resumes/export-zip` downloads approved
  tailored resumes/cover letters as a `.docx` zip. It does **not** export
  prose, conversation history, preferences, feedback, or logs.
- **Scoped resets/deletes:** experience preferences
  (`DELETE /experience/preferences`), the experience conversation
  (`POST /experience/conversation/reset`, which clears prose + optimized
  doc + embeddings), per-job feedback, and individual targets/jobs.

What's missing (tracked in
[#29](https://github.com/danieljoffe/wyrdfold/issues/29) as follow-up
implementation):

- A single account-deletion path that cascades across DB rows, both
  storage buckets, embeddings, and the cost/notification logs.
- A full personal-data export ("download everything I've given you").
- A documented retention policy for the append-only logs.

Until those land, an operator removing a person's data must do it
manually: delete the user's rows across the per-user tables above and the
`{user_id}/` prefixes in both storage buckets, then confirm in the
OpenRouter and Sentry dashboards per those providers' retention settings.

## Operator responsibilities

If you self-host WyrdFold, you are the data controller. At minimum:

- Keep **Zero Data Retention enabled** on your OpenRouter account and
  rotate the key if it's ever exposed.
- Treat the Supabase **service-role key** as the keys to all user data;
  it bypasses row-level security (see
  [SECURITY.md](./SECURITY.md) and
  [#79](https://github.com/danieljoffe/wyrdfold/issues/79)).
- Decide your own retention policy and honor deletion/export requests
  manually until the automated flows ship.
- If you enable Sentry, review what your exceptions carry before pointing
  the DSN at a shared Sentry org.
