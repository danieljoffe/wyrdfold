import os
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.llm import ModelId

# Tests set WYRDFOLD_API_TESTING=1 in conftest before importing the app
# so the developer's real `.env` (with experimental flags like
# RECENCY_DECAY_ENABLED / PHASE1_TRIAGE_ENABLED) can't leak into the
# test process and silently switch code paths. See #28.
_TEST_ENV_FILE: str | None = None if os.environ.get("WYRDFOLD_API_TESTING") == "1" else ".env"


class Settings(BaseSettings):
    # extra="ignore": unknown keys in the dotenv file must not crash boot —
    # self-hosters commonly keep unrelated vars (PORT, tooling keys) in .env.
    model_config = SettingsConfigDict(
        env_file=_TEST_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # Deployment-modes epic (docs/plan-wyrdfold-deployment-modes.md, Phase 2).
    # The mode gates ONLY the perimeter (signup/provisioning/billing) — never
    # the data model, RLS, or auth mechanics, which are identical in both.
    #   self_host — closed signup; the owner is provisioned at boot from
    #               OWNER_EMAIL (see app/services/owner_provisioning.py).
    #   saas      — open signup + billing perimeter (Phase 3; no behavior yet).
    # Default self_host: the safe posture for anyone who clones the repo.
    deployment_mode: Literal["self_host", "saas"] = "self_host"
    # First-run owner bootstrap (self_host only): when set, boot idempotently
    # creates this auth user (email-confirmed) so the operator can sign in via
    # magic link without ever opening the Supabase dashboard. Unset = no-op,
    # so existing deployments are unaffected.
    owner_email: str = ""

    supabase_url: str = ""
    supabase_service_role_key: str = Field(default="", repr=False)
    # Anon (publishable) key — the base for the per-request, JWT-bound
    # client that RLS enforcement runs through (#79). Distinct from the
    # service-role key (which bypasses RLS). Only required once per-user
    # data access migrates onto the user client; unset is fine until then.
    supabase_anon_key: str = Field(default="", repr=False)
    wyrdfold_api_key: str = Field(default="", repr=False)
    # Dedicated cron/automation key (#29 round 3 / H4). OPTIONAL, default
    # empty. When set, it is accepted by ``verify_api_key`` (the strictly
    # operator/cron routes: /poll, /discovery, /admin, jobs rescore +
    # backfill-salary, sources POST/seed, targets funnel) IN ADDITION to
    # ``wyrdfold_api_key`` — but it is deliberately NOT accepted by
    # ``verify_api_key_or_jwt`` (the six user-data routers). This gives the
    # operator a migration path to a narrowly-scoped automation credential:
    # point cron/poller/batch at WYRDFOLD_CRON_KEY (works only on operator
    # routes), then the broad WYRDFOLD_API_KEY can be retired so a leak of
    # the automation key can no longer authenticate against user data. Empty
    # changes nothing (the legacy key keeps working everywhere it does today).
    wyrdfold_cron_key: str = Field(default="", repr=False)
    # Shared secret proving a request came through the trusted Next.js BFF
    # (SEC-5). The BFF injects it as ``X-Wyrdfold-BFF`` on the public,
    # IP-rate-limited endpoints (waitlist join, signup-mode); the API requires
    # it there so a direct hit to Railway can't spoof ``X-Forwarded-For`` to
    # rotate past the per-IP limit. OPTIONAL: empty disables the check
    # (fail-open) so a deploy that hasn't set it on BOTH platforms yet doesn't
    # hard-break public signup. Rollout: set the BFF (Vercel) var first so it
    # starts sending the header, then the API (Railway) var to begin enforcing.
    wyrdfold_bff_secret: str = Field(default="", repr=False)
    # JWT verification uses Supabase's JWKS endpoint at
    # `<supabase_url>/auth/v1/.well-known/jwks.json` — public-key verification
    # with key rotation handled automatically. No shared secret required.
    # Override the audience for tests; production should keep "authenticated".
    supabase_jwt_audience: str = "authenticated"
    greenhouse_delay_ms: int = Field(default=200, ge=0, le=10_000)
    score_normalizer: int = 30
    allowed_hosts: str = ""

    # HTTP rate limiting (slowapi). In-memory backend — sufficient while the
    # API runs as a single Railway replica. Switch to Redis when scaling to
    # multiple replicas, otherwise limits become per-instance and bypassable.
    # Tests disable via RATE_LIMIT_ENABLED=false in conftest.
    rate_limit_enabled: bool = True

    # Sentry — leave DSN empty to disable (local dev, tests).
    sentry_dsn: str = Field(default="", repr=False)
    sentry_environment: str = "development"
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)

    # Verbose 500 bodies — FAIL-CLOSED. The unhandled-exception handler
    # returns a generic body by DEFAULT; raw exception text (which can carry
    # SQL fragments, PostgREST detail, file paths, or secrets) is only echoed
    # to the client when this is explicitly opted into. Previously the gate
    # keyed off ``sentry_environment == "production"``, which defaults to
    # "development" and is unset in deploy config — so prod was fail-OPEN and
    # leaked exception detail to any caller who triggered a 500 (audit #29
    # round 3 / H5). Set DEBUG_ERRORS=true ONLY in local/dev debugging.
    debug_errors: bool = False

    # Twilio SMS — set all three to enable SMS notifications (#511).
    twilio_account_sid: str = ""
    twilio_auth_token: str = Field(default="", repr=False)
    twilio_phone_number: str = ""

    # LLM provider — "anthropic" uses the Anthropic SDK direct; "openrouter"
    # routes the same Anthropic-shaped calls through OpenRouter (one billing
    # relationship, optional cross-provider fallback). Mock is the safe
    # default for tests + local dev. See
    # plan-wyrdfold-openrouter-migration.md for the migration roadmap.
    llm_provider: Literal["mock", "anthropic", "openrouter"] = "mock"
    anthropic_api_key: str = Field(default="", repr=False)
    anthropic_timeout_seconds: float = Field(default=600.0, ge=1.0, le=3600.0)
    # Bumped from 2 → 5 alongside the V3 prompt rollout. The default-2 budget
    # exhausts on a small fraction of cases during burst load (e.g. Phase 2
    # backfill grading dozens of jobs in under a minute); 5 gives Anthropic
    # rate-limit retries enough headroom to recover without sacrificing
    # responsiveness. Each retry uses exponential backoff inside the SDK.
    anthropic_max_retries: int = Field(default=5, ge=0, le=10)

    # OpenRouter (PR A of plan-wyrdfold-openrouter-migration.md). Drop-in
    # replacement for the Anthropic SDK that routes through
    # https://openrouter.ai. ZDR is enabled account-wide in the OR
    # dashboard, not per-request.
    openrouter_api_key: str = Field(default="", repr=False)
    openrouter_timeout_seconds: float = Field(default=600.0, ge=1.0, le=3600.0)
    openrouter_max_retries: int = Field(default=3, ge=0, le=10)

    # LLM credit-runway alarm. Three OpenRouter credit drains (2026-06-25,
    # 2026-07-04, 2026-07-13) were each discovered only AFTER grading
    # silently died: the daily budget caps bound the burn RATE, but nothing
    # watched the BALANCE, and a 402-dead pipeline spends $0/day — which
    # looks exactly like a quiet one. When the operator key pays
    # (llm_provider="openrouter"), the poll-cycle health check probes the
    # key's remaining credit and alarms when the runway (remaining ÷
    # trailing 7-day daily spend) drops below this many days. 0 disables
    # the runway rule.
    llm_credit_min_runway_days: float = Field(default=3.0, ge=0.0)
    # Absolute floor on remaining credit, in USD: alarm whenever the
    # balance is below this regardless of run rate (protects the
    # rate-can't-be-computed cases — a fresh stats window, or a pipeline
    # already starved to ~$0/day). 0 disables the floor rule.
    llm_credit_min_remaining_usd: float = Field(default=2.0, ge=0.0)
    # Early warning for a SELF-RESETTING key cap (a $N/day key limit):
    # alarm once this fraction of the current window's cap is consumed.
    # Runway-days is the wrong lens for a resetting cap — it reads
    # permanently low (cap ÷ trailing rate), so the 2026-08-13 daily-cap
    # exhaustion alarmed only at $0.00, after the pipeline was already
    # dead, and kept alarming after the cap was raised. The fraction rule
    # fires while there is still budget to act on (warning at the
    # threshold, error at exhaustion). 0 disables it.
    llm_credit_key_cap_alert_fraction: float = Field(default=0.8, ge=0.0, le=1.0)

    # BYOK (#5). Master key for AES-256-GCM envelope encryption of
    # per-user provider API keys at rest in `user_api_keys`. Base64 of
    # exactly 32 random bytes (`openssl rand -base64 32`). Empty disables
    # BYOK storage entirely — the keys service refuses to encrypt/decrypt,
    # so single-tenant self-hosters who never set it are unaffected (they
    # use the operator env keys above). NOT interchangeable with the
    # Supabase service-role key; rotating it orphans all stored ciphertext.
    byok_master_key: str = Field(default="", repr=False)

    # BYOK (#5 P2). When True, a logged-in user with no stored OpenRouter
    # key is refused (HTTP 402 "add your key") rather than billed to the
    # instance key — the hosted-multi-tenant posture, so strangers can't
    # spend the operator's credits. Default False keeps single-tenant
    # self-host working untouched: missing user key → fall back to the
    # operator env key above. Has no effect in mock mode or for api-key /
    # cron callers (background spend is gated per payer in the poller).
    byok_require_user_keys: bool = False

    # ---- Stripe billing (Phase 3 slice 3; saas mode only) -------------
    # Secret API key (sk_test_/sk_live_). Empty disables the billing
    # routes entirely (they 404) — the self_host default: a self-hosted
    # instance has no subscriptions.
    stripe_secret_key: str = Field(default="", repr=False)
    # Webhook endpoint signing secret (whsec_...). The webhook route
    # refuses everything until this is set — never process an unsigned
    # billing event.
    stripe_webhook_secret: str = Field(default="", repr=False)
    # Recurring monthly Price ids for the managed tiers (test + live mode
    # each have their own). Unknown/unmapped prices in webhook events are
    # ignored, never guessed.
    stripe_starter_price_id: str = ""
    stripe_pro_price_id: str = ""

    # URL validation — enable to validate job URLs during polling.
    validate_poll_urls: bool = True

    # JSON-LD baseSalary fallback (#503): when a NEW row's JD text yields no
    # salary, fetch its hosted posting page and read schema.org
    # ``baseSalary`` (Lever/Ashby pages often carry structured pay their
    # board APIs omit). Ships dark; the per-source cap bounds the extra
    # fetch fan-out a cycle can add.
    jsonld_salary_enabled: bool = False
    jsonld_salary_max_fetches: int = 10

    # Periodic job URL health checks (see app/services/url_health.py).
    # 2026-07-31 cadence fix: batch 50 → 250 (HEADs are free; full live-corpus
    # sweep ~every 2 weeks) + the due-ordering RPC now serves rows carrying
    # strikes FIRST, so a dead URL archives in ~threshold days instead of
    # never (the pre-fix cascade had archived 0 jobs ever — audit doc).
    # Off by default. When enabled, the scheduler ticks every
    # ``url_health_tick_hours`` and HEAD-checks the oldest
    # ``url_health_batch_size`` live jobs. Jobs that fail
    # ``url_health_failure_threshold`` consecutive checks (4xx or network
    # error) get archived and their heavy fields NULL'd to reclaim space.
    url_health_check_enabled: bool = False
    url_health_tick_hours: int = Field(default=24, ge=1, le=720)
    url_health_batch_size: int = Field(default=250, ge=1, le=500)
    url_health_concurrency: int = Field(default=10, ge=1, le=50)
    url_health_failure_threshold: int = Field(default=3, ge=1, le=10)

    # Conversation-history window sent to the LLM on each orchestrated turn
    # (#29 audit: handle_turn previously loaded up to 1M turns and re-sent the
    # whole history every turn — unbounded token growth + eventual context
    # overflow for long-lived accounts). 50 = ~25 exchanges, comfortably above
    # any real onboarding conversation today; the full history stays persisted,
    # only the LLM window is capped.
    conversation_history_max_turns: int = Field(default=50, ge=1, le=1000)

    # Default job-list relevance floor (#60 workstream D). When a user hasn't
    # set their own ``user_profiles.list_min_score`` (NULL), the /jobs list
    # filters to scores >= this by default so the list surfaces solid matches
    # rather than the full keyword-noise tail. Not-yet-graded ("Pending") rows
    # are always exempt (they carry no real fit score yet), and the per-view
    # ``min_score`` chip always overrides it. An explicit stored 0 opts a user
    # out entirely; set this to 0 to disable the default floor instance-wide.
    # 40 = the "solid match" band (aligns with the #89 pre-scan cutoff).
    default_list_min_score: int = Field(default=40, ge=0, le=100)

    # Synthetic "mock" board provider for local load testing (#57). When True,
    # a source row with ``provider='mock'`` synthesizes a deterministic job
    # feed in-process (no network) so a poll burst is reproducible without
    # hammering real ATS boards. OFF by default and must stay off in prod: a
    # mistyped provider on a real source must fail its poll, not fabricate
    # jobs. See app/services/mock_board.py.
    mock_fetcher_enabled: bool = False

    # Retention purge for append-only operational logs (#29 P3). OFF by
    # default — opt-in via RETENTION_PURGE_ENABLED, so self-host keeps
    # every row until an operator chooses a window. When on, the scheduler
    # ticks every ``retention_purge_tick_hours`` and deletes rows older
    # than the per-table window below. A window of 0 days means "keep
    # indefinitely" (that table is skipped). External cron can call
    # ``POST /admin/retention/purge`` instead of running APScheduler.
    # See app/services/retention.py.
    retention_purge_enabled: bool = False
    retention_purge_tick_hours: int = Field(default=24, ge=1, le=720)

    # Activation staleness sweep (#557 §3 / #649). `deriving` and `polling` are
    # IN-FLIGHT states — a detached task is supposed to be advancing them. When
    # that task dies nothing notices, and the target is stranded (prod had one
    # sitting in `polling` for 27 days). This sweep converges such rows back to
    # `idle`, the re-activatable state, so they heal on the user's next visit.
    # Opt-in, like every other sweep here (retention/url_health/discovery): a
    # self-hosted deployment should not get unrequested background writes. The
    # one-shot heal in 20260811020000 fixes rows already stranded; this flag is
    # the ONGOING guard, so prod wants ACTIVATION_SWEEP_ENABLED=true.
    activation_sweep_enabled: bool = False
    activation_sweep_tick_hours: int = Field(default=6, ge=1, le=168)
    # Deliberately generous: the cutoff keys on `updated_at`, which a running
    # pipeline does not touch BETWEEN status transitions, so a tight window
    # could reclaim a live activation. Hours, not minutes.
    activation_stale_after_hours: int = Field(default=6, ge=1, le=720)
    # llm_costs.created_at feeds the rolling budget windows (≤30d) and the
    # cost/insights history, so the floor is a year; 0 = keep forever.
    llm_costs_retention_days: int = Field(default=365, ge=0)
    # notifications_sent.sent_at is the alert-dedup ledger; 180d is well
    # past any posting's active life. 0 = keep forever.
    notifications_sent_retention_days: int = Field(default=180, ge=0)
    # search_events.occurred_at — the search-funnel metrics ledger (#467
    # §10 PR6). `query` is user input, so bounded retention is part of the
    # privacy posture; 90d covers funnel iteration. 0 = keep forever.
    search_events_retention_days: int = Field(default=90, ge=0)
    # phase1_rejections.judged_at — the persistent Phase-1 negative-verdict
    # store. Rows older than the read TTL (phase1_rejection_ttl_hours,
    # default 60d) are dead weight the read path already ignores; sweep at
    # TTL + slack so a TTL raise never races the sweep. Also collects rows
    # stranded under stale profile_versions. 0 = keep forever.
    phase1_rejections_retention_days: int = Field(default=90, ge=0)

    # Firecrawl — set API key to enable JS-rendered page extraction fallback.
    firecrawl_api_key: str = Field(default="", repr=False)

    # Embeddings provider — set to "voyage" to use the real SDK; mock is the default.
    embeddings_provider: Literal["mock", "voyage"] = "mock"
    voyage_api_key: str = Field(default="", repr=False)
    voyage_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    voyage_max_retries: int = Field(default=2, ge=0, le=10)

    # Phase 1 LLM title triage. When True, the poller's ingestion-time
    # gate uses the Haiku-backed binary classifier in
    # ``app/services/relevance/title_triage.py`` instead of the legacy
    # cosine prefilter (which proved structurally weak for short job
    # titles — see plan-llm-scoring-migration.md). Ships FALSE so the
    # PR can be validated per-target in DEV before flipping on. When
    # False the poller is pass-through (no gate); precision relies on
    # downstream keyword scoring.
    phase1_triage_enabled: bool = False

    # Phase 1 admission gate on the model's own confidence (0-100). A
    # ``promising`` verdict the model is only guessing at (confidence below
    # this) is dropped — the confidence signal gates admission, not just
    # Phase-2 ordering (#47). 40 drops only the prompt's "you're guessing"
    # band (0-39); NULL-confidence (legacy / pre-confidence) verdicts are
    # exempt and still admit, preserving the lean-promising default.
    phase1_min_confidence: int = Field(default=40, ge=0, le=100)

    # Which model backs Phase-1 title triage. Default Haiku (proven incumbent,
    # Anthropic-shaped via OpenRouter). Set PHASE1_TRIAGE_MODEL=deepseek-v3-2 to
    # route triage through OpenRouter's OpenAI-compatible path — 13x cheaper and
    # higher agreement-with-oracle in the bake-off, but slower. A safe env flip:
    # revert instantly by unsetting it, no deploy. Grading (Sonnet) is unaffected.
    phase1_triage_model: ModelId = "claude-haiku-4-5"

    # ---- Phase-1 backfill at target activation (#930) ----------------------
    #
    # Phase-1 triage only ever judges listings whose ``external_id`` is NOT
    # already in the catalog, and ``bulk_title_score_for_target`` (the
    # activation fan-out) writes keyword-only ``stage1`` rows with no
    # ``promising`` at all. Together: a job admitted without a Phase-1 verdict
    # never gets one, so a freshly activated target shows an UNJUDGED list.
    # This backfill closes that hole — newest-first, age-bounded, bounded by
    # the daily count cap below.
    #
    # Ships FALSE. Turning it on is a visible STEP CHANGE, not a refinement:
    # the pass writes real ``promising=False`` rows, which set
    # ``excluded`` and REMOVE listings from the target's /jobs view. That is
    # the point (judging means rejecting), but measured on prod the majority
    # of a target's keyword-matched rows are Phase-1 negatives, so flip it
    # per-deploy and watch — same shape as ``phase1_triage_enabled`` and
    # ``persistent_block_admits_ingestion``.
    phase1_backfill_enabled: bool = False

    # How far back the backfill grades, in days since ``jobs.cataloged_at``.
    # Deliberately the same clock AND the same default as
    # ``archival_archive_after_days`` (30): archival soft-archives on
    # ``cataloged_at``, so grading past this bound buys verdicts for listings
    # that disappear from every view before anyone reads them. Measured on
    # prod 2026-08-31: only ~1,540 of a target's ~8,200 ungraded score rows
    # are inside the window — the bound removes ~81% of the candidate set for
    # zero user-visible loss.
    phase1_backfill_max_age_days: int = Field(default=30, ge=1, le=365)

    # Phase-1 triage calls per TARGET per UTC day — the count ceiling Phase 2
    # has had all along as ``phase2_daily_cap`` and Phase 1 never did (only
    # the global daily budget and the payer's monthly allowance bounded it).
    # Sourced from ``llm_costs`` exactly like the Phase-2 cap, so the
    # activation backfill and ordinary poll-cycle ingestion draw on ONE
    # counter.
    #
    # This is a RUNAWAY BACKSTOP, not a rationing device — size it above real
    # traffic. Measured on prod: the busiest day in the last 10 was 573 calls
    # for a single target (2026-08-24), so 1000 is ~1.7x the observed peak and
    # inert against today's behaviour while still catching a loop. 0 disables.
    phase1_daily_cap: int = Field(default=1000, ge=0)

    # The share of ``phase1_daily_cap`` ONE activation backfill may consume.
    # Backfill and fresh ingestion share the cap, so without this a large
    # activation could spend the whole day's ceiling and starve the day's new
    # intake. At 0.25 a backfill can take at most a quarter of the day's calls
    # and three quarters are structurally reserved for fresh listings. A full
    # age-bounded backfill measures ~8 calls per target, so this bounds a
    # pathological case, not the ordinary one. 0 disables backfill spending
    # entirely; the cap being 0 (disabled) leaves the backfill unbounded by
    # count, exactly as Phase 1 is today.
    phase1_backfill_cap_fraction: float = Field(default=0.25, ge=0.0, le=1.0)

    # ---- Catalog-health observability (#958) --------------------------------
    # Product-level eyes on the intake funnel: one catalog_health_cycles row
    # per recorded poll cycle (window intake, corpus quality, admitted-title
    # token histogram) plus a distribution tripwire that would have caught the
    # #952 class ("thousands ingested, none relevant") within a cycle or two.
    catalog_health_enabled: bool = True
    # Trailing intake window each row measures. 24h keeps the token sample
    # large enough to be stable at current intake rates (~hundreds/day).
    catalog_health_window_hours: int = Field(default=24, ge=1)
    # Rows are recorded at most this often — the due-source path can tick far
    # more frequently than the 30-minute poll cadence.
    catalog_health_min_interval_minutes: int = Field(default=30, ge=0)
    # Token histogram size stored per row and compared by the tripwire.
    catalog_health_top_tokens: int = Field(default=15, ge=1)
    # Baseline = up to this many prior rows whose window ended before the
    # current window began (zero overlap, so the comparison is honest).
    catalog_health_baseline_cycles: int = Field(default=48, ge=1)
    # Tripwire fires when the total-variation distance between the window's
    # and the baseline's normalized token distributions exceeds this. TV is in
    # [0, 1]; 0.5 means "half the probability mass moved" — a regime change,
    # not noise. Deliberately loud-only: a WARNING log + a flagged row.
    catalog_health_tripwire_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # Below these sample sizes the tripwire records "not evaluated" instead of
    # guessing from noise (title counts, current window / baseline combined).
    catalog_health_min_sample_titles: int = Field(default=20, ge=1)
    # Rows older than this are pruned by the recorder itself (48/day → tiny,
    # but unbounded tables are how "temporary" telemetry becomes debt).
    catalog_health_retention_days: int = Field(default=90, ge=1)

    # How often to RESUME Phase-1 backfills that stopped early. 0 disables the
    # sweep entirely (the activation-time pass still runs).
    #
    # The backfill is allowed to stop early by design — its own fractional
    # allowance, the shared daily cap, the global budget, or an unavailable
    # provider. The rows it did not reach stay ``promising IS NULL``, and
    # NOTHING ELSE WILL EVER JUDGE THEM: ordinary polling only triages
    # externally-new listings, which is the entire reason this backfill exists.
    # Before this sweep, ``_activate_pipeline`` was the only caller, so a pass
    # cut short by a cap stayed cut short until a user happened to toggle the
    # target off and on. Review caught it; the invariant is that an early stop
    # must have an automatic path to eventual completion.
    #
    # DAILY is the right cadence, not per-cycle. Three of the four stop reasons
    # (own allowance, shared daily cap, global daily budget) only free up at
    # the UTC rollover, so resuming sooner cannot spend more — it would just
    # re-page the window for nothing, and a no-work pass reads every page of
    # the 30-day window (~3s per target). Each day grants another
    # ``phase1_backfill_cap_fraction`` share, so even a target needing several
    # thousand calls converges in days rather than never.
    phase1_backfill_resume_tick_hours: int = Field(default=24, ge=0, le=720)

    # Recency decay (#5). When True the /jobs list sorts/paginates by
    # ``scores.recency_score`` (the fit score decayed by posting age via
    # ``app/services/recency.py``) and the poller refreshes that column
    # each cycle. When False the multiplier is 1.0 (recency_score ==
    # score) and the list sorts by raw fit score exactly as before — the
    # flag is a pure sort change, safe to flip per-deploy.
    recency_decay_enabled: bool = False

    # Periodic full recency-score sweep (#5). The poller's per-cycle refresh
    # only re-derives ``recency_score`` for jobs it re-fetched that tick, so a
    # posting that ages off the boards freezes at its last-refresh decay while
    # its true age keeps climbing — the list sort key goes stale relative to
    # the read-time displayed decay. When enabled, the scheduler ticks every
    # ``recency_refresh_tick_hours`` and rewrites ``recency_score`` for ALL
    # live (non-excluded) score rows from the current date, keeping the stored
    # sort key consistent with what users see. Off by default; pairs with
    # ``recency_decay_enabled`` (a no-op identity write when decay is off).
    recency_refresh_enabled: bool = False
    recency_refresh_tick_hours: int = Field(default=12, ge=1, le=720)

    # Phase 2 LLM job-fit grading (#6). When True the poller runs the
    # Sonnet-backed ``score_with_phase2_and_persist`` over promising
    # (Phase 1) jobs in place of the legacy Stage 3 keyword+LLM blend,
    # progressively batched and bounded by the per-target daily cap. When
    # False the poller runs the legacy Stage 3 path unchanged. Phase 2
    # only grades rows Phase 1 marked ``promising``, so it requires
    # ``phase1_triage_enabled`` to surface any work.
    phase2_enabled: bool = False

    # Faithfulness review pass for generated resumes (#6b). When True the
    # tailor pipeline runs a second LLM "review" over the freshly generated
    # resume, flags claims the source experience doesn't support, and (on
    # medium/high-severity flags) regenerates ONCE with the flags as a
    # critique — the corrective run is not re-reviewed. Ships FALSE: it adds
    # up to ~2 LLM calls per resume, so flip it on per-deploy to A/B the
    # quality lift against the spend.
    faithfulness_review_enabled: bool = False

    # Job qualification firewall (#60). When True the poller runs the
    # target-INDEPENDENT qualification tagger
    # (``app/services/qualification/``) over each newly-ingested job — one
    # cheap Haiku call per job, AFTER the US filter and BEFORE per-target
    # scoring — and writes the intrinsic tags (is_us, role_family,
    # seniority, employment_type, metro, is_remote, is_genuine_role) onto
    # the ``jobs`` row so per-target grading can pre-filter cheaply. Ships
    # FALSE so merging the package + migration triggers NO LLM spend; the
    # tagger is best-effort (failures never break polling) and the
    # content-hash (``jobs.qualified_hash``) skips re-tagging unchanged
    # rows. Flip per-deploy once validated in DEV.
    qualification_enabled: bool = False

    # Which model backs the qualification tagger. Default Haiku — the proven
    # incumbent, pinned into the prompt-regression golden contract so a swap
    # can't merge silently. Set QUALIFICATION_MODEL=deepseek-v3-2 to route
    # through OpenRouter's OpenAI-compatible path: the tagger is the single
    # largest LLM spender (41% / ~$52 of the 2026-07 30-day anatomy) and its
    # one-job-per-call output (~170 tokens) sits far inside deepseek's
    # ceilings, so unlike Phase-1 triage it needs no batch clamp. Tagging is
    # best-effort per job (a failed call leaves tags NULL for a later
    # re-attempt), so a model hiccup degrades gracefully, never breaks
    # polling.
    qualification_model: ModelId = "claude-haiku-4-5"

    # Which model backs Phase-2 fit grading (``fit.job`` — the four-axis score
    # users see). Default Sonnet (the incumbent the score distribution and
    # gate calibrations were built on). Set PHASE2_FIT_MODEL=deepseek-v3-2 for
    # the 2026-07-15 bake-off winner among the cheap candidates: Spearman
    # rho 0.917 vs the production Sonnet gold over 58 real cases (Sonnet's own
    # re-grade self-agreement is 0.974, Sonnet-4.5's 0.924), 0 schema
    # failures, $0.00045/call vs $0.0114 (~25x cheaper). Known trade-offs,
    # measured: slight score-band compression (stdev 25.5 vs 27.7), occasional
    # 15-20pt under-scores on middle-band cases, ~27s latency (fine here —
    # fit.job is background-only via the poller; it has no interactive
    # callers). gemini-2.5-pro (11/58 failures) and gpt-5.1 (rho 0.875,
    # over-scores) were eliminated. Env flip, instant unset-to-revert; new
    # grades mix with the Sonnet-scored corpus until rows naturally re-grade
    # (profile bumps), so expect a mildly mixed scale during transition.
    phase2_fit_model: ModelId = "claude-sonnet-4-6"

    # Max characters of the (cleaned) job description sent to the
    # qualification tagger. The tagger only needs the dense signals near the
    # top of a JD (country cues, seniority wording, "general application"
    # boilerplate, contract/intern wording) to produce its target-INDEPENDENT
    # tags; sending the full body burned ~3.4K input tokens/call grinding the
    # ~16k backlog (the June overspend). A short snippet (~600 chars ≈ 150
    # tokens) preserves tag quality at a fraction of the per-call input cost.
    # Set higher only if a tag-quality regression appears; 0 means "send no
    # description" (title/company/location only).
    qualification_jd_snippet_chars: int = Field(default=600, ge=0)

    # deepseek-v3-2 won the bake-off on quality-per-dollar (100% valid JSON,
    # ~6x the recall of the sub-cent tier, ~1/14th of Sonnet's cost). Pinned
    # into the prompt-regression golden so a swap can't merge silently.
    skills_extraction_model: ModelId = "deepseek-v3-2"

    # US-only corpus (#60 workstream B). When on, the qualification tagger
    # ARCHIVES (stamps ``archived_at``) a job the instant it tags it
    # high-confidence non-US — closing the loop between the L2 ``is_us``
    # verdict and the ``archived_at`` gate the display/query layer already
    # honors, so non-US postings don't linger in a catalog a US-only product
    # never surfaces (and stop being re-polled/re-graded). Reversible
    # (``archived_at`` is nullable) and conf-gated by the threshold below to
    # protect a mistagged US role. OFF by default: a self-host that wants a
    # global catalog leaves it off, and the tagger still records ``is_us`` for
    # anyone who'd rather filter on it than archive.
    qualification_archive_non_us: bool = False
    # Talent-pool / "general application" / evergreen NON-postings (#60,
    # schema-audit wire-up 2026-07-31): the tagger's ``is_genuine_role=false``
    # verdict archives the row in the same firewall write — a non-posting has
    # no business in the corpus (prod had 139 of them being served in public
    # search). Lenient by construction: only an explicit ``false`` archives;
    # ``None`` (malformed/absent verdict) keeps the row. Reversible
    # (archived_at flag), default ON.
    qualification_archive_non_genuine: bool = True
    # Minimum ``us_confidence`` (0-100) for the archive above to fire. 80 keeps
    # the tagger's genuinely-uncertain calls (which include most US
    # false-negatives) live; a prod sample of the >=80 set was 100% non-US.
    qualification_non_us_archive_min_confidence: int = Field(default=80, ge=0, le=100)

    # Backfill sweep (#285): the per-cycle tagger only sees jobs re-upserted
    # THIS cycle, so a job that fell off its source's feed without being
    # archived stays untagged forever and slips through the is_us (#257) /
    # role_family (#278) read gates on the NULL benefit-of-the-doubt. Each
    # scheduled cycle, liveness-check up to this many of the OLDEST untagged,
    # unarchived jobs, then TAG the live ones (budget-gated inside the same
    # tagger path — respects the grading reserve, so it can't starve grading)
    # and ARCHIVE the dead ones. Each row costs one HTTP liveness check, so keep
    # the batch modest to bound the poll cycle's added latency. 0 disables. Only
    # runs when ``qualification_enabled``.
    qualification_backfill_batch: int = Field(default=50, ge=0, le=1000)

    # Phase-2 grade backfill (the qualify-sweep's grading twin). Every poll
    # cycle, grade up to this many LIVE, view-ordered ``promising`` rows per
    # (user, target) that are stuck at ``stage2`` — deferred grades and the
    # rows a profile-version bump reset (which otherwise re-grade only if
    # their source happens to re-list them; a profile edit wiped one target's
    # graded shelf and left its /jobs list 76% off-target pending noise,
    # 2026-07-15). Spend is bounded by every existing gate (global breaker,
    # payer allowances, BYOK require-mode, and the per-target DAILY quota the
    # sweep shares with cycle grading — it fills unused quota, never exceeds
    # it). 0 disables (default: grading spends payer money, so self-host
    # opts in; prod sets PHASE2_BACKFILL_BATCH).
    phase2_backfill_batch: int = Field(default=0, ge=0, le=500)

    # ---- Archival lifecycle (UX/IA §5; Stage 1 + Stage 2 A/B) ----------
    # Ships OFF like every sweep — the operator flips ARCHIVAL_SWEEP_ENABLED
    # per-deploy. When on, a throttled (~6h) sweep piggybacks the poll cycle:
    #   Stage 1: jobs older than ``archival_archive_after_days`` that NO user
    #     has engaged with (every user_jobs status is new/archived — absent
    #     rows count as 'new') get ``archived_at`` stamped. Reversible; the
    #     default list views already exclude archived rows, and the existing
    #     ``status='archived'`` view + direct links keep them reachable.
    #   Stage 2: rows archived longer than ``archival_purge_after_days`` are
    #     either HARD-DELETED (no engagement, no graded history, and delisted
    #     by their source for ``archival_delist_grace_days`` — pure noise; FK
    #     children cascade) or TOMBSTONED (``purged_at`` + payload stripped)
    #     when anything is worth keeping or the source still lists them —
    #     a tombstone blocks poller re-insert, so purged jobs can't reappear.
    # Batches are bounded per phase per run (IO discipline, 2026-07-10).
    archival_sweep_enabled: bool = False
    archival_archive_after_days: int = Field(default=30, ge=7, le=365)
    archival_purge_after_days: int = Field(default=60, ge=30, le=730)
    archival_delist_grace_days: int = Field(default=14, ge=1, le=90)
    archival_sweep_batch: int = Field(default=500, ge=0, le=5000)

    # Pre-scan job embeddings (#60, Phase 1). When True the poller embeds
    # each newly-ingested / changed job ONCE (target-INDEPENDENT) and caches
    # the vector in ``job_embeddings`` via
    # ``app/services/embeddings/job_embeddings.py``. PURELY the populate
    # side — no gating, no behavior change: nothing reads these vectors yet.
    # Ships FALSE so merging the table + hook triggers NO embedding spend;
    # the write is best-effort (an embedding error never breaks polling) and
    # content-hash cached (an unchanged re-poll re-embeds nothing). Requires
    # ``EMBEDDINGS_PROVIDER=voyage`` + ``VOYAGE_API_KEY`` to embed for real;
    # with the mock provider it writes deterministic fake vectors. Since the
    # Disk IO slim-down (2026-07-30) this gates the LAZY path only: vectors
    # materialize at grade time (``ensure_job_vectors`` in the Phase-2
    # runner) for exactly the candidate set — there is no embed-on-ingest.
    prescan_embed_enabled: bool = False

    # DEPRECATED (Disk IO slim-down, 2026-07-30): the vector-less-job sweep
    # (#21) is gone — lazy grade-time embedding makes that stranding class
    # structurally impossible (a job embeds exactly when first needed). The
    # field survives so a stale env var doesn't break settings parsing;
    # nothing reads it.
    prescan_embed_backfill_batch: int = Field(default=200, ge=0, le=2000)

    # Logistics extraction (plan-wyrdfold-logistics-chips.md). When True
    # the Phase 2 grader's system prompt includes a section asking the
    # model to emit a `logistics` JSON object (remote_status, salary
    # min/max/currency/unit, location_city/country) alongside the axis
    # scores. The result is persisted to ``scores.logistics_filters``
    # (migration #20260603100000) and consumed by the /jobs logistics
    # filters/chips (#86). Logistics is FILTER-ONLY — never affects
    # score / recency_score / sort order.
    #
    # Flipped ON 2026-06-30 after the shadow-run check the older comment
    # asked for: an A/B over 5 JDs across the fit range (base prompt vs
    # base+addendum) gave fit-score Δ mean +1.4, max |Δ| 4, ranking
    # preserved — score-neutral within grader sampling noise. Historical
    # scores are NOT backfilled (per the user); the column populates on
    # newly-graded jobs going forward.
    #
    # Flipped back OFF 2026-08-18 (#846): the boards publish these facts and we
    # now read them. Ashby/Lever/SmartRecruiters state remote, employment type
    # and country outright; Greenhouse/Workday state remote in the location
    # string, which ``location_parse`` already reads. Paying a reasoning model
    # to infer the employer's own answer bought nothing — and the /jobs filter
    # already PREFERS the deterministic columns, keeping the grader's output as
    # a fallback that covers "graded rows only" (~4% for country, per
    # ``routers/jobs.py``). So this turns off a minority fallback, not the
    # primary source. Salary is unaffected: that filter reads jobs.salary_*,
    # parsed deterministically from the posting's own salary text.
    logistics_extraction_enabled: bool = False

    # Skills harvest (plan-phase2-structured-harvest.md): the Phase-2 grader
    # additionally emits skills_required / skills_matched / skills_missing —
    # structured, normalized, capped lists mined from the SAME read that
    # produces the grade (marginal cost ≈ output tokens only). Purely
    # informational, never a score input; the eval re-baseline that shipped
    # this flag proved band stability with the addendum on. Fields persist to
    # the scores row (pair-level, denormalized for the insights aggregation —
    # the analyses-only source covered ~146 rows ever). It does NOT write
    # jobs.skills_required: that column is the search facet, owned solely by
    # the skill dictionary so the vocabulary stays canonical.
    # Historical grades are NOT backfilled; columns populate as jobs (re)grade.
    skills_harvest_enabled: bool = True

    # Learner re-score projection / learning-rate cap (#5 P4). Before a
    # high-confidence ``ProfilePatch`` auto-applies, the learner projects the
    # patch over the target's recent scored jobs (deterministic keyword
    # re-score) and stages it for review instead of applying when the
    # projected churn is an outlier — so one learn run can't silently reshuffle
    # the whole list. All four are tunable knobs.
    #
    # A job "moves" when its projected blended score changes by at least this
    # many points (blend is 60% keyword / 40% LLM, so a keyword-only delta is
    # scaled by 0.6 before comparison).
    learning_rescore_move_threshold: int = Field(default=20, ge=1, le=100)
    # The patch is an outlier (→ stage) when this fraction of considered jobs
    # move by >= the threshold.
    learning_rescore_max_moved_fraction: float = Field(default=0.30, ge=0.0, le=1.0)
    # Cap how many recent scored jobs the projection re-scores (bounds cost).
    learning_rescore_sample_size: int = Field(default=150, ge=1, le=2000)
    # Don't apply the cap until the target has at least this many scored jobs —
    # a brand-new target has too little signal to judge "outlier" against, and
    # shouldn't have its first patches blocked.
    learning_rescore_min_jobs: int = Field(default=10, ge=1, le=1000)

    # Anonymous voting on reference-JD contributions (#5 P3). A contribution is
    # suppressed from the shared-profile merge once its NET down-votes
    # (down minus up) reach this quorum; re-merged without it.
    contribution_downvote_quorum: int = Field(default=3, ge=1, le=100)

    # Cap on reference-JD contributions a single user can add to one target
    # (#47). Bounds a rogue contributor's footprint on the shared scoring
    # profile, on top of the per-contributor merge de-bias + downvote
    # suppression. Soft cap (count-then-insert; the route's 10/min rate limit
    # + de-bias make the rare race harmless).
    reference_jd_max_per_user_per_target: int = Field(default=5, ge=1, le=100)

    # Email/SMS notifications — Next.js app URL and shared secret for job alerts.
    next_app_url: str = ""
    job_alert_secret: str = Field(default="", repr=False)

    # Slow-request log threshold (ms). Requests slower than this get logged
    # at WARNING with method/path/duration. Set to 0 to log every request.
    #
    # 750, not 500: on the current (small) prod instance a healthy authed read
    # is ~500-650ms (query + the API-to-Supabase round-trip + payload + JWT), so
    # a 500ms bar flagged nearly every request -- noise, not signal. 750 flags
    # the genuine anomalies (the /jobs family at 2.7-9s, /analysis LLM ~26s) and
    # stays quiet on baseline reads (measured 2026-07-23). Prod already runs this
    # via the SLOW_REQUEST_THRESHOLD_MS env override; this makes it the default so
    # a cleared env var doesn't silently revert to the noisy 500. Lower it again
    # once the instance is upsized and the baseline drops.
    slow_request_threshold_ms: int = Field(default=750, ge=0, le=60_000)

    # Application log format (#26 F5). `text` keeps stdlib/uvicorn
    # defaults for local DX; `json` attaches a JSON formatter to the
    # root logger so log-aggregation tools can index each field. See
    # app/logging_config.py.
    log_format: Literal["text", "json"] = "text"
    #: Root log level. Separate from ``log_format`` on purpose — they were
    #: entangled once and production lost every INFO record for it (#862).
    #: Tunable without a deploy so verbosity can be raised during an incident.
    log_level: str = "INFO"

    # CORS — comma-separated allowlist of origins permitted to call the API
    # from a browser. Empty disables CORS (server-to-server only). Production
    # should be the Next.js app URL; local dev typically `http://localhost:3000,http://localhost:3100`.
    cors_allowed_origins: str = ""

    # In-process scheduled poller. Off by default so tests and ad-hoc dev
    # processes don't trigger background fetches; ops opt-in via env var.
    # Tick = how often the scheduler wakes up to look for due sources;
    # actual per-source cadence is governed by ``sources.poll_interval_minutes``.
    poll_scheduler_enabled: bool = False
    poll_tick_minutes: int = Field(default=30, ge=1, le=1440)
    # Watchdog: max wall-time for ONE poll cycle before it's aborted so the
    # next tick can run. A hung cycle (e.g. a stuck httpx/LLM await during an
    # upstream outage) otherwise wedges the scheduler indefinitely —
    # APScheduler's ``max_instances=1`` won't start a new tick while the old
    # one is still "running", so a hang stops ALL polling until a restart
    # (exactly the 402-storm incident on 2026-07-06: 68 min of no polls while
    # the API stayed up). ``asyncio.wait_for`` cancels the cycle at this bound;
    # the advisory lock unwinds and the next tick recovers. Keep it below the
    # tick interval so an aborted cycle doesn't overlap the next. 0 disables
    # (wait forever — the pre-watchdog behavior).
    poll_cycle_timeout_seconds: int = Field(default=1200, ge=0)
    # Max sources polled per cycle (#514 residual). An unbounded due batch
    # interacts badly with the watchdog above: a backlog cycle (restart,
    # cron gap) tries every due source at once, blows past
    # ``poll_cycle_timeout_seconds``, the abort kills the unfinished tail
    # un-stamped, and the next cycle repeats the exact same oversized batch —
    # the fleet starves in a loop instead of draining. Measured 2026-07-29:
    # 1,110 of 3,231 enabled sources >2x overdue, 1,077 unpolled for 24h+.
    # Capping the batch to what a cycle can actually finish (and taking the
    # most-overdue first) drains the backlog cap-sized-chunk by chunk.
    # 0 = legacy unbounded. (The original sizing note said 250 comfortably
    # fits the watchdog; live 2026-08-05 showed it does NOT when the batch
    # holds giant boards — ~75/cycle landed and every tick aborted. The
    # per-source budget below is the fix; the cap stays as the batch bound.)
    poll_max_sources_per_cycle: int = Field(default=250, ge=0)
    # Per-source wall-time budget inside a cycle (2026-08-05). The watchdog
    # + cap bound the CYCLE, but one giant board (a workday tenant with
    # hundreds of detail fetches behind 429 backoff) can occupy one of the
    # POLL_CONCURRENCY slots for many minutes — observed live: every
    # overnight tick blew the 1200s watchdog, cancelling every in-flight
    # source, while only ~75 of the 250-source batch finished. The budget
    # cancels ONE source, not the cycle: the worker stamps last_polled_at
    # (so the board rotates to the back of the due queue instead of
    # re-hogging a slot next tick), the stale-archive pass is skipped by
    # construction (the cancel lands before it — a partial fetch must never
    # mass-archive live rows), and the cycle keeps polling. Chronically
    # over-budget boards surface via the WARNING log = catalog-hygiene
    # candidates. Keep it well under poll_cycle_timeout_seconds. 0 disables.
    poll_source_budget_seconds: int = Field(default=300, ge=0)
    # TTL for the persistent Phase-1 rejection store (#514; the
    # ``phase1_rejections`` table — see
    # docs/plan-phase1-rejection-persistence.md). A rejected candidate never
    # ingests, so the same title re-enters triage every poll of its source
    # until the posting closes; the store remembers the "no" per
    # (target, profile_version, normalized title) so it isn't re-bought.
    # Profile edits bump profile_version, which re-judges everything under
    # the new profile immediately — the TTL exists only to bound staleness
    # against prompt/model drift, NOT as the primary lifecycle. Its
    # in-process predecessor defaulted to 24h and re-billed the entire
    # standing rejected corpus daily (~75-90% of Phase-1 volume, measured
    # 2026-08-12); 1440h = 60 days. 0 disables the store entirely.
    phase1_rejection_ttl_hours: float = Field(default=1440.0, ge=0.0)
    # Postgres advisory-lock key for the scheduled poll. A single stable
    # bigint so only ONE poll runs at a time across every replica AND the
    # Vercel cron — pg_try_advisory_lock returns false to a second caller,
    # which skips cleanly. Arbitrary but fixed; change it only if it ever
    # collides with another advisory lock in the same database.
    poll_advisory_lock_key: int = 8675309

    # Ingestion auto-recovery + health alerting (poll-outage hardening).
    # The Sept-2026 outage went unnoticed for 10+ days: the only poll
    # trigger (a daily Vercel Hobby cron) broke, every source then tripped
    # the failure backoff and was disabled the same day, and nothing
    # alerted. These settings stop that recurring.
    #
    # Auto-recovery: a source the backoff auto-disabled is re-enabled (and
    # its consecutive_failures reset) once its ``disabled_at`` is older
    # than this many hours, so a transient ATS-wide outage can't kill
    # ingestion forever. The sweep runs from the poll cycle. 0 disables
    # recovery (sources stay disabled until an operator intervenes).
    source_recovery_after_hours: int = Field(default=24, ge=0, le=8760)
    # Health check (runs from the scheduler/poll cycle). Fires a Sentry
    # alert when ingestion looks dead — the symptom that went unnoticed.
    # Off-switch is the threshold being 0.
    ingestion_health_check_enabled: bool = True
    # "No new jobs in N hours" — the highest-value alert. max(jobs.created_at)
    # older than this fires. 48h is comfortably inside a healthy daily
    # cadence yet catches a stall long before the 10-day blind spot. 0
    # disables this check.
    ingestion_max_job_age_hours: int = Field(default=48, ge=0, le=8760)
    # Mass-disable alert: fires when this fraction (or more) of all sources
    # are currently disabled — the other face of the outage (every source
    # backed off at once). 0 disables this check.
    ingestion_mass_disable_ratio: float = Field(default=0.5, ge=0.0, le=1.0)

    # Brave Search API — powers the target-driven source-discovery loop. Set
    # the key to enable; empty key disables discovery entirely (the service
    # logs a warning and exits cleanly). 2,000 free queries/month is plenty
    # for daily-per-target with a query cap. Get one at https://brave.com/search/api/.
    brave_search_api_key: str = Field(default="", repr=False)
    # PER-TARGET ceiling on Brave queries in one discovery run — applied
    # INDEPENDENTLY inside each ``run_discovery_for_target`` so a single
    # keyword-rich target can't hog a pass. It does NOT bound total monthly
    # usage by itself: N targets each fire up to this many, so the run total
    # scales with target count. The run-TOTAL budget below is the real
    # monthly-cost control. The target's keyword x site plan is shuffled then
    # truncated to this, so repeated runs sample the whole space cursorlessly.
    discovery_query_cap_per_run: int = Field(default=200, ge=1, le=2000)
    # Per-keyword result depth — top N URLs we look at from each search.
    discovery_results_per_query: int = Field(default=20, ge=1, le=50)
    # Run-TOTAL Brave query budget shared across ALL targets in one bulk pass
    # (``run_discovery_all_targets``) — the monthly-cost control, and INVARIANT
    # to target count (unlike the per-target cap above). The budget is split
    # across the run's targets (shuffled for fairness across runs); once spent,
    # the remaining targets are deferred to the next run. Monthly Brave usage
    # ~= this x runs/month, so the free tier (2,000/month) holds by
    # construction: the default 60 with the daily scheduler ≈ 1,800/month —
    # reaching the free tier with headroom, regardless of how many targets
    # exist. 0 disables the run budget (each target then uses only its
    # per-target cap — the legacy behavior that scales with target count).
    discovery_query_budget_per_run: int = Field(default=60, ge=0, le=20000)

    # Per-user cap on boards a single account can register by creating targets
    # from job URLs (the from-url flow — see source_registration). Sources are
    # global + forever-polled, so this bounds the cost a single account can
    # impose: at most this many DISTINCT boards per user. Discovery-inserted
    # sources aren't attributed to a user and don't count against it.
    source_registration_cap_per_user: int = Field(default=25, ge=0, le=1000)

    # Lazy fit-score refresh (E2): max stale targets recomputed per /targets/mine
    # view. Bounds the LLM cost one page load can trigger; the rest refresh on
    # subsequent views. 0 disables the lazy refresh entirely.
    fit_score_refresh_max_per_view: int = Field(default=3, ge=0, le=50)

    # Concurrency cap for the pre-scan embedding write fan-out (job_embeddings
    # HNSW upserts). Much lower than DB_WRITE_CONCURRENCY (12) on purpose: HNSW
    # index inserts largely serialize internally + are IO-heavy, so a wide
    # fan-out just piles contention on a small instance and STARVES foreground
    # reads (the /jobs statement-timeouts, 2026-07-23) without real throughput.
    # A few in flight keeps ingestion moving while leaving IO for user reads.
    # DEPRECATED (2026-07-30): the on-ingest embed fan-out this bounded is
    # gone (lazy grade-time embedding batches internally); kept for env-var
    # compatibility, nothing reads it.
    embedding_write_concurrency: int = Field(default=3, ge=1, le=32)

    # In-process scheduled source discovery. Off by default (same posture as
    # the poll scheduler) so tests and ad-hoc dev processes don't fire Brave
    # queries; ops opt-in via env var. When enabled the scheduler ticks every
    # ``discovery_tick_hours`` and runs a discovery pass across ALL targets
    # (active + inactive) so a dormant target's boards keep refreshing. The
    # Brave-key gate still applies inside the run — an empty
    # ``brave_search_api_key`` makes each per-target pass a clean no-op, so
    # enabling this flag without a Brave key does nothing. Tick is hours
    # (discovery is a daily-cadence job, not minutes like the poll).
    discovery_scheduler_enabled: bool = False
    discovery_tick_hours: int = Field(default=24, ge=1, le=720)
    # Discovery-staleness alarm — checked on the ingestion-health tick (which
    # piggybacks the poll cycle). ARMED ONLY when ``discovery_scheduler_enabled``
    # is on, so a deliberately-off discovery loop never pages; when armed it
    # raises a Sentry ``warning`` if the newest ``source_discoveries.discovered_at``
    # is older than this many hours. This is the guard for the exact silent
    # freeze that motivated #60: discovery stopped producing new sources and
    # nobody noticed for weeks (the catalog rotted until a user hit an empty
    # metro). The effective threshold is ``max(this, discovery_tick_hours * 2)``
    # so a single missed tick never pages and a longer tick auto-relaxes it. 0
    # disables just this check.
    discovery_max_age_hours: int = Field(default=48, ge=0, le=8760)
    # Postgres advisory-lock key for the bulk discovery run. A DISTINCT bigint
    # from ``poll_advisory_lock_key`` so a discovery pass and a poll never
    # contend on the same lock — they guard different work. Like the poll key
    # it serializes discovery across every replica AND the manual ``POST
    # /discovery/run`` trigger: a second caller gets ``false`` from
    # pg_try_advisory_lock and skips cleanly. The same generic
    # try_poll_advisory_lock / release_poll_advisory_lock RPCs back both keys.
    discovery_advisory_lock_key: int = 8675310
    # Leaked-advisory-lock alarm (#350) — checked on the ingestion-health
    # tick. The poll/discovery locks are session-level and live on PostgREST's
    # pooled backend, so an API death mid-cycle leaks them and every later
    # poll silently skips. Postgres records no lock-acquisition time, so the
    # check pairs "lock currently held" (advisory_lock_info RPC) with
    # behavioral staleness: alarm when a lock is held while the newest
    # sources.last_polled_at / source_discoveries.discovered_at is older than
    # this many minutes. Sized comfortably above a legitimate long hold (the
    # poll watchdog caps cycles at poll_cycle_timeout_seconds = 20min; a full
    # force-poll stamps sources continuously as it goes). 0 disables.
    advisory_lock_stale_after_minutes: int = Field(default=90, ge=0, le=10080)

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # Per-user LLM budget (defense-in-depth). Rolling window over llm_costs.
    # Set to 0 to disable a window. API-key callers (cron) bypass the HTTP
    # gate, but background work is charged to the target's activator and
    # gated against their monthly allowance in the poller.
    user_llm_daily_budget_usd: float = Field(default=5.0, ge=0.0)
    # Global LLM circuit breaker (defense-in-depth above the per-user
    # gates). When the day's total spend across ALL users (UTC midnight
    # window, every llm_costs row) reaches this cap, the poll cycle's
    # budget gate goes empty: every target's LLM work defers until the
    # next UTC day while jobs keep ingesting fail-open. Catches runaway
    # background spend that per-user allowances can't (many users, or
    # mis-attributed system rows). 0 disables.
    global_llm_daily_budget_usd: float = Field(default=10.0, ge=0.0)
    # Slice of ``global_llm_daily_budget_usd`` fenced off for live grading
    # (Phase-1 triage + Phase-2 fit) that the background qualification tagger
    # must NOT consume. The tagger is the dominant LLM spender and, sharing one
    # budget meter with grading, repeatedly drained the day's budget (and the
    # OpenRouter key) before grading ran — starving the product's actual output
    # (jobs pile up ungraded at ``stage2``). With a reserve the tagger stops at
    # ``global_llm_daily_budget_usd - reserve`` while grading reads the full
    # cap, so grading always has at least this much headroom. Must be <
    # ``global_llm_daily_budget_usd`` to leave the tagger any room; >= it makes
    # the tagger yield entirely (grading-only). 0 = old shared-pool behavior.
    grading_budget_reserve_usd: float = Field(default=3.0, ge=0.0)
    user_llm_hourly_budget_usd: float = Field(default=1.0, ge=0.0)
    # The overall allowance (Claude-limits model: small windows above for
    # bursts, this for the month). Rolling 30 days; counts ALL of a user's
    # llm_costs — interactive and background alike. Per-user override via
    # user_profiles.llm_monthly_budget_usd (the manual "add credits" lever).
    # In saas mode this is the fallback for users outside a managed tier;
    # managed tiers use the plan budgets below (interactive-only counting).
    user_llm_monthly_budget_usd: float = Field(default=5.0, ge=0.0)
    # Per-PAYER daily ceiling on BACKGROUND LLM work (Phase 1 triage + Phase 2
    # fit), rolling 24h, charged to whoever activated the target.
    #
    # Distinct from ``user_llm_daily_budget_usd`` above, which gates INTERACTIVE
    # HTTP requests a user makes themselves. Background work is unattended and
    # automatic, so it needs its own ceiling: that setting's own comment records
    # that background spend "is charged to the target's activator and gated
    # against their MONTHLY allowance in the poller" -- monthly and nothing else.
    #
    # The gap that leaves: the monthly allowance bounds the TOTAL but not the
    # RATE, so a payer could burn a whole month in a day. Phase 1 had no
    # per-user cap of any kind, and ``phase2_daily_cap`` is per TARGET -- a user
    # following N targets got N x that quota while the only per-user ceiling
    # stayed monthly.
    #
    # Dollars, not call counts, deliberately: a call cap cannot bound cost when
    # model prices move, and ours did -- the fallback rate table was wrong by
    # ~3x for one model until #934. Metering reuses the ``cost_log`` totals
    # ``PayerBudgetGate`` already reads for the monthly window, so this is one
    # more query per payer per cycle, not a parallel accounting path.
    #
    # Covers BOTH phases through one mechanism, because both consult the same
    # gate (Phase 1 via ``target_blocked``, Phase 2 via ``user_block_reason``).
    # It does NOT cover target-INDEPENDENT work billed to the instance key --
    # that has no payer to charge and is bounded by
    # ``global_llm_daily_budget_usd``. That distinction is precisely what let
    # the qualification tagger burn on unseen by the per-payer gate.
    #
    # Default sizing: against a $5 monthly allowance, 0.50/day spreads a full
    # month over at least ten days. Typical single-target usage sits far below
    # it (``phase2_daily_cap`` alone bounds a target to a small fraction), so it
    # binds exactly the many-targets case it exists for. 0 disables.
    payer_daily_budget_usd: float = Field(default=0.50, ge=0.0)
    # Phase 3 tiers (saas mode only; app/services/entitlements.py resolves
    # user_profiles.plan → these). Managed-tier quotas count INTERACTIVE
    # purposes only — background (triage/fit-grading/polling) is bounded
    # structurally by the per-tier active-target caps, not by dollars.
    # Pricing locked 2026-07-03: Starter $7/mo → $2 quota + 2 targets,
    # Pro $19/mo → $6 quota + 5 targets; free = BYOK + 1 target.
    starter_monthly_billable_budget_usd: float = Field(default=2.0, ge=0.0)
    pro_monthly_billable_budget_usd: float = Field(default=6.0, ge=0.0)
    free_max_active_targets: int = Field(default=1, ge=1)
    starter_max_active_targets: int = Field(default=2, ge=1)
    pro_max_active_targets: int = Field(default=5, ge=1)
    # Trial tier (#841): a new account runs on HOST keys so onboarding works,
    # bounded by BOTH a total spend ceiling and a duration.
    #
    # The ceiling is the real control and the duration is the backstop — an
    # idle trial still costs money every poll cycle, so time-boxing is what
    # stops background spend on an account that has stopped converting.
    #
    # Unlike starter/pro this budget counts BACKGROUND purposes too
    # (``PlanEntitlements.quota_excluded_purposes is None``): a paid tier
    # excludes them because a subscription is already paying for them, which
    # a trial has nothing behind. Excluding them here would leave the ceiling
    # bounding only the cheap half.
    #
    # The window is NOT a new mechanic — the budget guard's rolling window is
    # ``MONTHLY_WINDOW_DAYS`` (30) and the trial is far shorter, so "spend in
    # the last 30 days" IS "spend during the trial" for any trial account.
    # Keep ``trial_days`` well under that or the ceiling stops being a total.
    #
    # Defaults are deliberately conservative — the repo is public, so the real
    # values belong in the environment, not here.
    trial_days: int = Field(default=3, ge=1)
    trial_billable_budget_usd: float = Field(default=1.0, ge=0.0)
    trial_max_active_targets: int = Field(default=1, ge=1)
    # On-click deep job analysis: max LLM-backed runs per user per rolling
    # 24h. Cache hits don't write llm_costs rows, so re-views stay free.
    analysis_daily_limit: int = Field(default=20, ge=0)
    # Grade CATALOG-ONLY targets — ``app_active`` rows with no active
    # ``user_targets`` link, i.e. seed targets nobody is pursuing. Their
    # Phase-1/Phase-2 grading bills the INSTANCE key, and measured against
    # prod it is the dominant line item while serving nobody: the scores it
    # writes are re-derived anyway by the activation fan-out
    # (``bulk_title_score_for_target``, "runs at target activation so postings
    # that pre-date the target still appear").
    #
    # Default False = seed targets still POLL (sources fetched, jobs ingested,
    # discovery runs — the catalog keeps growing, which is what they exist for)
    # but are not LLM-graded until a real user activates them.
    #
    # SAFE because public ``/search`` reads the ``jobs`` table and skips
    # ``scores`` entirely (``services/job_search.py``) — ``promising`` does not
    # gate the public corpus. NB an EARLIER rule ("never spend money nobody
    # will consume") did starve that corpus, but it dropped catalog targets
    # from the ACTIVE SET, killing their source polling. This flag is narrower
    # by construction: it gates grading only and never touches polling.
    grade_catalog_targets: bool = False
    # Max concurrent backgrounded tailoring runs per user (#656). Backgrounding
    # the ~39s resume pipeline removed the serialization a blocking request
    # imposed on a browser tab, and `enforce_llm_budget` meters spend whose
    # `llm_costs` rows don't land until each run's LLM returns — so N
    # simultaneous kicks would all read the same pre-burst spend and all pass.
    # Dedup already collapses repeat kicks for the SAME document; this bounds
    # a fan-out across different postings. 0 disables. `/tailor/batch` is the
    # sanctioned bulk path and has its own rate limit.
    tailor_max_concurrent_runs: int = Field(default=3, ge=0)
    # Phase 2 grading quota per target per UTC day (was a hardcoded 100 in
    # daily_cap.py — at ~$0.0035/call that alone exceeded a $5 monthly
    # allowance; 20/day ≈ $2/month/target).
    phase2_daily_cap: int = Field(default=20, ge=0)

    # Phase 2 seniority pre-gate (#902). When True, candidates whose title is
    # clearly below the target's ``seniority_hint`` are dropped before Phase 2
    # spends a Sonnet grade on them (shadow-measured: ~32% of grades skipped
    # for a director target, 94% of them genuine waste). Only gates targets
    # hinted director-or-above; ambiguous titles always pass. Ships False so
    # the skip volume can be validated per-target before enforcing.
    phase2_seniority_gate_enabled: bool = False
    # Allowed rungs below the hint (1 = a Manager still grades for a Director
    # target — the stretch case — but a Coordinator does not).
    phase2_seniority_gate_tolerance: int = Field(default=1, ge=0, le=6)

    # Idle-account lifecycle. last_seen_at is stamped on authenticated
    # requests (throttled in-process); the poller defers a payer's LLM
    # work after idle_defer_days unseen and the lifecycle sweep
    # auto-deactivates their targets after idle_deactivate_days. 0
    # disables each stage. Tracking off in tests via conftest.
    activity_tracking_enabled: bool = True
    idle_defer_days: int = Field(default=7, ge=0)
    idle_deactivate_days: int = Field(default=30, ge=0)
    # Auto-disable a source after this many consecutive fetch failures
    # (0 disables the backoff).
    # When every active target is blocked for a PERSISTENT reason (idle payer,
    # unsponsored catalog target, disabled account), should new listings still
    # be admitted on the deterministic free gates alone?
    #
    # FALSE (default) ships the fix dark. Flipping it to True is a STEP CHANGE,
    # not a gradual one: prod measured ~14,800 free-gate survivors currently
    # unadmitted, which roughly DOUBLES the live catalog on the first pass or
    # two before settling to the ordinary intake rate. Land the admission ramp
    # first, then flip this per-deploy and watch — same shape as
    # ``phase1_triage_enabled`` and ``qualification_enabled``.
    #
    # While False, a persistent block still stalls new-job ingestion, which the
    # ingestion-health alert reports honestly rather than hiding.
    persistent_block_admits_ingestion: bool = False

    # Ramp for the admission that flag opens. Bounds how many previously
    # unadmitted listings enter the catalog per POLL CYCLE, so a backlog drains
    # over days rather than arriving in one tick and dragging a burst of score
    # rows, activation-time tagging and archival behind it.
    #
    # Only the persistent-block FALLBACK consumes this. A job admitted the
    # ordinary way — a target actually triaged it and said promising — is never
    # rate-limited, and neither is a cycle with triage off or no targets.
    #
    # 0 disables the cap. RAISED 50 -> 500 (2026-09-01) because the assumption
    # this number was sized against stopped being true, and the sentence that
    # used to live here — "steady-state intake (~100-200/day) never touches the
    # ceiling" — was measurably wrong within a day:
    #
    #   * It was sized to drain a ONE-OFF ~14,800-row backlog gently. But with
    #     no active user targets, EVERY admission takes the persistent-block
    #     fallback, so this stopped being a backlog throttle and became the
    #     total intake ceiling for the whole system.
    #   * ``admit_on_any_target`` widened the eligible pool. Traced against live
    #     boards: ~417 NEW eligible listings per cycle, against 50 slots — so
    #     ~367 were deferred every cycle and a given listing had roughly a
    #     1-in-8 chance of ever landing. Frontend roles (about 1 new per cycle)
    #     effectively never won that lottery, which is what made /search look
    #     stale for a whole role family.
    #   * ``intake_max_new_jobs_per_hour`` (2000/h) now does the burst
    #     protection this was improvised for, properly and at the right layer.
    #     It sat at ~5% utilisation while THIS ceiling bound every single cycle,
    #     which is the tell that the wrong one was doing the work.
    #
    # 500/cycle at a 30-minute tick is ~24,000/day, comfortably above measured
    # eligible supply so listings stop queueing indefinitely, while the hourly
    # ceiling above stays the real backstop. Deliberately NOT uncapped: two
    # ceilings at very different scales is what hid this for a day, and one
    # notch with a measurement beats removing the guard outright.
    persistent_block_admission_cap_per_cycle: int = Field(default=500, ge=0)

    # Ceiling on how many NEW listings AUTOMATED intake may add to the catalog
    # per rolling hour. Automated intake means the poller: the scheduled cycle,
    # ``POST /poll/due``, ``poll_all_sources`` and the target-activation
    # fan-out. All four share this one budget.
    #
    # Distinct from ``persistent_block_admission_cap_per_cycle`` above, which
    # throttles only the persistent-block FALLBACK: a job a target actually
    # triaged and called promising has never been rate-limited at all, so
    # before this setting nothing bounded ordinary intake. That was survivable
    # only because supply happened to be low; a discovery run that adds
    # sources, or a large board publishing a backlog, could drive an unbounded
    # insert burst, and each new row drags score rows, embeddings and archival
    # work behind it.
    #
    # USER-INITIATED materialization is deliberately NOT gated by this.
    # ``job_ingest.materialize_and_score_job`` — behind ``POST /jobs/manual``
    # and target-creation-from-a-JD-URL — inserts ONE row per request, is
    # rate-limited at the endpoint, and represents explicit human intent.
    # Refusing it to protect against a POLLER burst would invert the priority
    # this codebase already sets elsewhere: ``grading_budget_reserve_usd``
    # fences off budget so background work yields to live work, never the
    # reverse. A person clicking "add this job" must not fail because the
    # poller spent the hour.
    #
    # Those rows are still COUNTED: they land in ``jobs.cataloged_at`` like any
    # other, so they shrink the allowance the next poll cycle reads. The hour
    # can therefore be exceeded, but only by human action, and only by the
    # trickle a rate-limited one-row-per-request path can produce. The burst
    # vectors this exists for are all on the automated side.
    #
    # Placed AFTER the relevance gates, so it never changes WHICH listings are
    # worth admitting — only how fast already-judged-worthy ones land.
    # Deferred listings are not lost: the next poll of the same source re-sees
    # them. Known rows are never counted or blocked either — a content refresh
    # (JD edit, salary re-extraction, the escaped-HTML heal) updates a row that
    # already exists and adds no insert pressure of the kind this bounds.
    #
    # Truth is re-read from ``jobs.cataloged_at`` at the start of each cycle
    # rather than kept in a process counter, so it survives restarts and the
    # overlapping-cycle case that broke the admission ramp's first draft
    # (``POST /poll/due`` runs without the scheduler's advisory lock). Two
    # cycles overlapping can therefore overshoot by at most one cycle's intake
    # before the next re-read corrects it — bounded, and far below the headroom
    # this cap leaves.
    #
    # 2000/hour is ~20x the observed steady-state intake, so it never binds in
    # normal operation and exists purely as a burst ceiling. 0 disables.
    intake_max_new_jobs_per_hour: int = Field(default=2000, ge=0)

    # Whether a listing may enter the catalog on ANY target's keywords, or only
    # on an ACTIVE target's.
    #
    # These are two different questions that the poller answered with one set
    # for a long time. "Which listings belong in the shared catalog that public
    # /search reads?" is a CORPUS question. "Which targets do we spend LLM money
    # grading against?" is a SPEND question. Tying the first to the second means
    # the public corpus silently shrinks to whatever a handful of targets happen
    # to be activated for — and grows or shrinks as people activate and
    # deactivate, which is not a property anyone asked for.
    #
    # Concretely, and the reason this exists: the catalog held four INACTIVE
    # frontend targets while the five active ones covered backend, full-stack,
    # PM, design, data and devops. So "Frontend Engineer" matched nothing at the
    # door and /search could never return a recent frontend role, no matter how
    # long it ran. The keywords existed; nothing consulted them.
    #
    # Admission stays keyword-bounded either way -- this widens the keyword set,
    # it does not open the door. Measured on 22,730 real listings from 12 live
    # boards: 20.0% pass on active targets alone, 39.9% on all targets, so the
    # catalog roughly DOUBLES rather than exploding toward the ~211,000 the
    # boards collectively list.
    #
    # SPEND IS UNAFFECTED. Phase 1 and Phase 2 keep grading against ACTIVE
    # targets only; this flag governs the free deterministic gate and nothing
    # else. A row admitted for an inactive target simply sits in the catalog
    # ungraded, which is exactly what /search reads (it skips ``scores``).
    admit_on_any_target: bool = True

    source_failure_disable_threshold: int = Field(default=10, ge=0)
    # #912: when the backoff is about to disable a source, re-detect the
    # company's ATS first. A 404 says the board TOKEN is stale, not that the
    # company stopped hiring — measured on prod, 27 of 139 failing sources
    # resolved to a different LIVE board (Notion/Ramp/Plaid/Sentry, all
    # Greenhouse -> Ashby) and another 33 were still live on the token we
    # already held. Off-switch for the probing this adds to the poll cycle:
    # false restores the pre-#912 behaviour (disable at the threshold, no
    # probes). See app/services/source_redetect.py.
    source_redetect_on_disable_enabled: bool = True
    # Cooldown on RE-VERIFYING a board a probe just confirmed live. A
    # ``still_live`` verdict suppresses the disable without resetting
    # ``consecutive_failures`` (the counter is real signal — the normal fetch
    # path IS still failing), so the source stays above the threshold and every
    # later failed poll would otherwise re-probe. 12h bounds the probe rate
    # while staying UNDER ``source_recovery_after_hours`` (24), so a board that
    # dies during a cooldown is re-verified and retired within one recovery
    # cycle rather than after it. 0 disables the cooldown (probe every time).
    source_redetect_still_live_cooldown_hours: int = Field(default=12, ge=0, le=168)
    # Adaptive source cadence. Sources whose ``last_candidate_at`` is
    # older than this many days get their poll interval stretched to
    # daily by the lifecycle sweep; sources that produce candidates
    # again get restored to the 4-hour default. NULL last_candidate_at
    # (pre-backfill rows) are left untouched. 0 disables the sweep step.
    source_cold_after_days: int = Field(default=7, ge=0)

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


settings = Settings()
