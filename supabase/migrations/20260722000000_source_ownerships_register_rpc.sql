-- From-url source registration (B): when a user creates a target from a job
-- URL on a supported ATS, register that company's board as a pollable source
-- so the poller pulls MORE jobs from that company going forward. Sources are
-- global (shared corpus), but each registration is attributed to a user and
-- capped, so a single account can't script thousands of URL pastes into an
-- unbounded, forever-polled source list (cost/abuse control).
--
-- The `sources` row itself already auto-enables + dedups on board_token (see
-- insert_source_if_not_exists / the poller). What's new here is the per-user
-- ownership ledger + the cap, both enforced inside one SECURITY DEFINER RPC so
-- a client can't bypass the cap with a direct insert.

-- ---------------------------------------------------------------------------
-- Ownership ledger: which user registered which source (distinct boards, not
-- distinct URLs — two postings on the same board dedup to one ownership row).
-- user_id is a bare uuid (not an auth.users FK), matching public.user_jobs.
-- ---------------------------------------------------------------------------
create table if not exists public.source_ownerships (
    user_id uuid not null,
    source_id uuid not null references public.sources(id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (user_id, source_id)
);

alter table public.source_ownerships enable row level security;

-- Read-only for the owner (a future "boards you added" view). NO insert/update/
-- delete policy or grant for authenticated: all writes go through the definer
-- RPC below so the cap can't be sidestepped by inserting ownership rows directly.
create policy "Users read their own source_ownerships" on public.source_ownerships
    for select to authenticated
    using ((select auth.uid()) = user_id);

grant select on public.source_ownerships to authenticated;
grant all on public.source_ownerships to service_role;

-- The PK (user_id, source_id) already indexes the per-user count + the
-- ownership existence check, so no extra index is needed.

-- ---------------------------------------------------------------------------
-- register_source_from_url: atomically upsert the source (auto-enabled) + the
-- caller's ownership row, holding them to a per-user cap. Returns a status:
--   already_owned  the caller already owns this board (idempotent no-op)
--   cap_reached    caller is at the cap for a board they don't yet own — no
--                  source and no ownership are created (full no-op)
--   registered     a brand-new source row was created + owned by the caller
--   linked         the source already existed (discovery / another user) and
--                  the caller now owns it too
--
-- SECURITY DEFINER + search_path '' so the cap is server-authoritative and
-- every reference is schema-qualified. The dead-board filter (job_count == 0)
-- and the ATS classification live in the Python caller (they need a network
-- probe); this function assumes an already-classified, live board.
-- ---------------------------------------------------------------------------
create or replace function public.register_source_from_url(
    p_user_id uuid,
    p_provider text,
    p_board_token text,
    p_company_name text,
    p_cap int
) returns text
    language plpgsql
    security definer
    set search_path to ''
as $$
declare
    v_source_id uuid;
    v_count int;
    v_inserted_source boolean := false;
begin
    select id into v_source_id from public.sources where board_token = p_board_token;

    -- Idempotent: re-pasting a board the caller already owns is a no-op, even
    -- if they're at the cap (they're not adding anything new).
    if v_source_id is not null and exists (
        select 1 from public.source_ownerships
        where user_id = p_user_id and source_id = v_source_id
    ) then
        return 'already_owned';
    end if;

    -- Soft cap: bounds distinct boards a single user can register. Not locked
    -- against a same-user race (two concurrent registrations could both pass at
    -- count = cap-1), which is fine for an abuse control with a fuzzy ceiling.
    select count(*) into v_count from public.source_ownerships where user_id = p_user_id;
    if v_count >= p_cap then
        return 'cap_reached';
    end if;

    if v_source_id is null then
        insert into public.sources (provider, board_token, company_name, enabled)
        values (p_provider, p_board_token, p_company_name, true)
        on conflict (board_token) do nothing
        returning id into v_source_id;
        if v_source_id is not null then
            v_inserted_source := true;
        else
            -- Lost the insert race — the row the other txn created is now visible.
            select id into v_source_id from public.sources where board_token = p_board_token;
        end if;
    end if;

    insert into public.source_ownerships (user_id, source_id)
    values (p_user_id, v_source_id)
    on conflict (user_id, source_id) do nothing;

    if v_inserted_source then
        return 'registered';
    end if;
    return 'linked';
end;
$$;

-- Cap-bearing write: service_role only (the from-url background task runs on
-- the service-role client). Supabase's default privileges auto-grant EXECUTE to
-- anon + authenticated on new public functions, so revoke from those roles
-- explicitly (not just PUBLIC) — matching the R2 score-write lockdown, and
-- enforced by tests/integration/test_privilege_invariants.py.
revoke all on function public.register_source_from_url(uuid, text, text, text, int)
    from public, anon, authenticated;
grant execute on function public.register_source_from_url(uuid, text, text, text, int) to service_role;
