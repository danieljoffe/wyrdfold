-- search_events: the public/authed search funnel's metrics ledger
-- (#467 §10 PR6 — volume / coverage-gap / conversion).
--
-- Privacy by construction: NO user_id column, NO ip column — the schema
-- cannot carry them, so a code regression can't quietly start recording
-- them. `query` is the normalized (trimmed/lowercased) search text — it
-- is user input and could incidentally contain personal text, which is
-- why the table is deny-all + retention-purged rather than kept forever.
--
-- Event kinds (CHECK-pinned so junk can't widen the vocabulary):
--   search       — a search executed (either surface; cache hits count).
--                  Carries query/result_count/has_more/filters, so
--                  zero-result queries surface the coverage gaps.
--   card_open    — a listing card was opened into the detail modal.
--   signup_click — the logged-out detail's "Sign up free" allusion was
--                  clicked: the public funnel's conversion tick.
--
-- `job_posting_id` (card_open) has deliberately NO foreign key: the
-- archival purge deletes job rows, and an analytics ledger must never
-- constrain — or be broken by — the corpus lifecycle.

create table if not exists public.search_events (
    id bigint generated always as identity primary key,
    occurred_at timestamptz not null default now(),
    event_type text not null
        check (event_type in ('search', 'card_open', 'signup_click')),
    -- Which surface emitted it. Beacon events carry the client's claim
    -- (analytics-grade, not authz); search events are stamped server-side.
    surface text not null check (surface in ('public', 'authed')),
    query text check (char_length(query) <= 120),
    result_count integer check (result_count >= 0),
    has_more boolean,
    location text check (char_length(location) <= 100),
    posted_within_days integer,
    page_offset integer check (page_offset >= 0),
    job_posting_id uuid
);

comment on table public.search_events is
    'Search-funnel metrics ledger (#467 §10): search volume, zero-result '
    'coverage gaps, card-open + signup-click conversion. Deliberately no '
    'user_id / no IP; retention-purged (search_events_retention_days).';

-- Purge path (delete .. where occurred_at < cutoff) + time-bucketed
-- one-shot analytics both scan by time.
create index if not exists search_events_occurred_at_idx
    on public.search_events (occurred_at);

-- Operational table for the service-role client only (deny-all: RLS on
-- with no policies + belt-and-braces privilege revoke).
alter table public.search_events enable row level security;
revoke all on table public.search_events from anon, authenticated;
