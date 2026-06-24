import os
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # URL validation — enable to validate job URLs during polling.
    validate_poll_urls: bool = True

    # Periodic job URL health checks (see app/services/url_health.py).
    # Off by default. When enabled, the scheduler ticks every
    # ``url_health_tick_hours`` and HEAD-checks the oldest
    # ``url_health_batch_size`` live jobs. Jobs that fail
    # ``url_health_failure_threshold`` consecutive checks (4xx or network
    # error) get archived and their heavy fields NULL'd to reclaim space.
    url_health_check_enabled: bool = False
    url_health_tick_hours: int = Field(default=24, ge=1, le=720)
    url_health_batch_size: int = Field(default=50, ge=1, le=500)
    url_health_concurrency: int = Field(default=10, ge=1, le=50)
    url_health_failure_threshold: int = Field(default=3, ge=1, le=10)

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
    # llm_costs.created_at feeds the rolling budget windows (≤30d) and the
    # cost/insights history, so the floor is a year; 0 = keep forever.
    llm_costs_retention_days: int = Field(default=365, ge=0)
    # notifications_sent.sent_at is the alert-dedup ledger; 180d is well
    # past any posting's active life. 0 = keep forever.
    notifications_sent_retention_days: int = Field(default=180, ge=0)

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

    # Recency decay (#5). When True the /jobs list sorts/paginates by
    # ``scores.recency_score`` (the fit score decayed by posting age via
    # ``app/services/recency.py``) and the poller refreshes that column
    # each cycle. When False the multiplier is 1.0 (recency_score ==
    # score) and the list sorts by raw fit score exactly as before — the
    # flag is a pure sort change, safe to flip per-deploy.
    recency_decay_enabled: bool = False

    # Phase 2 LLM job-fit grading (#6). When True the poller runs the
    # Sonnet-backed ``score_with_phase2_and_persist`` over promising
    # (Phase 1) jobs in place of the legacy Stage 3 keyword+LLM blend,
    # progressively batched and bounded by the per-target daily cap. When
    # False the poller runs the legacy Stage 3 path unchanged. Phase 2
    # only grades rows Phase 1 marked ``promising``, so it requires
    # ``phase1_triage_enabled`` to surface any work.
    phase2_enabled: bool = False

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

    # Logistics extraction (plan-wyrdfold-logistics-chips.md). When True
    # the Phase 2 grader's system prompt includes a section asking the
    # model to emit a `logistics` JSON object (remote_status, salary
    # min/max/currency/unit, location_city/country) alongside the axis
    # scores. The result is persisted to ``scores.logistics_filters``
    # (migration #20260603100000) and consumed by the /jobs logistics
    # chips. When False the prompt is unchanged and the column stays
    # NULL — this is the additive shadow that pre-flip rollout uses.
    # Per ``feedback-prompt-change-shadow-run``: ship behind this flag,
    # compare axis-score distributions before flipping in production.
    logistics_extraction_enabled: bool = False

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

    # Email/SMS notifications — Next.js app URL and shared secret for job alerts.
    next_app_url: str = ""
    job_alert_secret: str = Field(default="", repr=False)

    # Slow-request log threshold (ms). Requests slower than this get logged
    # at WARNING with method/path/duration. Set to 0 to log every request.
    slow_request_threshold_ms: int = Field(default=500, ge=0, le=60_000)

    # Application log format (#26 F5). `text` keeps stdlib/uvicorn
    # defaults for local DX; `json` attaches a JSON formatter to the
    # root logger so log-aggregation tools can index each field. See
    # app/logging_config.py.
    log_format: Literal["text", "json"] = "text"

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
    # Hard cap on total Brave queries fired per discovery run, across all
    # targets and keywords. The free tier is 2,000/month; at 200/day across
    # daily runs we'd burn through it in 10 days, so 200 is the ceiling for a
    # single run and the per-target loop fans out within that budget.
    discovery_query_cap_per_run: int = Field(default=200, ge=1, le=2000)
    # Per-keyword result depth — top N URLs we look at from each search.
    discovery_results_per_query: int = Field(default=20, ge=1, le=50)

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
    user_llm_hourly_budget_usd: float = Field(default=1.0, ge=0.0)
    # The overall allowance (Claude-limits model: small windows above for
    # bursts, this for the month). Rolling 30 days; counts ALL of a user's
    # llm_costs — interactive and background alike. Per-user override via
    # user_profiles.llm_monthly_budget_usd (the manual "add credits" lever).
    user_llm_monthly_budget_usd: float = Field(default=5.0, ge=0.0)
    # On-click deep job analysis: max LLM-backed runs per user per rolling
    # 24h. Cache hits don't write llm_costs rows, so re-views stay free.
    analysis_daily_limit: int = Field(default=20, ge=0)
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
    source_failure_disable_threshold: int = Field(default=10, ge=0)
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
