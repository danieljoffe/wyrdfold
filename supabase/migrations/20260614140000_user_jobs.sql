-- Per-user job pipeline status (#75 C1). Additive: jobs.status stays the
-- source of truth until a later phase cuts reads over.
create table if not exists public.user_jobs (
    user_id uuid not null,
    job_posting_id uuid not null references public.jobs(id) on delete cascade,
    status text not null default 'new',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, job_posting_id)
);

alter table public.user_jobs enable row level security;

create policy "Users access their own user_jobs" on public.user_jobs
    for all to authenticated
    using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);

grant select, insert, update, delete on public.user_jobs to authenticated;
grant all on public.user_jobs to service_role;

-- status history becomes per-user too
alter table public.status_log add column if not exists user_id uuid;

-- Backfill ONLY for a single-tenant DB (exactly one user). Multi-user DBs
-- can't attribute global status to a user, so skip (a later phase handles it).
do $$
declare v_user uuid;
begin
    if (select count(*) from public.user_profiles) = 1 then
        select user_id into v_user from public.user_profiles;
        insert into public.user_jobs (user_id, job_posting_id, status)
            select v_user, j.id, j.status
            from public.jobs j
            where j.status is not null
        on conflict (user_id, job_posting_id) do nothing;
        update public.status_log set user_id = v_user where user_id is null;
    end if;
end $$;
