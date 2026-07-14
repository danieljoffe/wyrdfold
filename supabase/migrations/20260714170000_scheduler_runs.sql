-- scheduler_runs: a persisted "last run" marker per long-interval
-- scheduler job (#327 follow-up).
--
-- Why: APScheduler's IntervalTrigger measures from PROCESS START, and this
-- app deploys near-daily — so every 12-24h job (url_health, retention
-- purge, recency refresh) effectively never fired; discovery only escaped
-- because source_discoveries doubles as its natural marker (PR #327
-- anchored it to that). The other jobs delete or rewrite rows and leave no
-- readable trace, hence this one-row-per-job ledger: boot-time catch-ups
-- read it to decide "overdue → run now / fresh → anchor next fire".
--
-- Stamped at run START (an attempt marker, matching the discovery
-- semantics): a crashing job waits one tick instead of refiring every
-- boot, which keeps crash-loops from turning into run storms.

create table if not exists public.scheduler_runs (
    job_id text primary key,
    last_run_at timestamptz not null,
    updated_at timestamptz not null default now()
);

comment on table public.scheduler_runs is
    'Last-attempt marker per long-interval scheduler job; read by boot-time '
    'catch-ups so deploys stop resetting 12-24h interval timers (#327 class).';

-- Operational table for the service-role client only.
alter table public.scheduler_runs enable row level security;
revoke all on table public.scheduler_runs from anon, authenticated;
