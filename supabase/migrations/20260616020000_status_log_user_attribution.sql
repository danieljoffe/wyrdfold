-- #113: finish status_log user attribution for multi-tenant.
--
-- status_log.user_id was added nullable in 20260614140000 with a backfill that
-- only ran on single-tenant DBs (one user_profile), leaving multi-user DBs with
-- NULL attribution and no follow-on. The app is meant for multiple users, so:
--   1. backfill any residual NULLs from user_jobs (status.py dual-writes both
--      with the same user_id, so the posting's user_jobs owner is the writer),
--   2. enforce NOT NULL — every status transition is a user action; the only
--      writer is the JWT-gated status router (the poller never touches
--      status_log), so an unattributed row is now a bug, not a valid state,
--   3. give status_log the per-user RLS read policy its siblings already have
--      (it had RLS enabled but NO policy — the one per-user table missing one),
--      so the per-request user client (#79) only sees its own transitions.
--
-- No new index: status_log is ~1 row at beta scale (#101) and the existing
-- (posting_id) / (created_at) indexes cover the read paths; a per-user index is
-- a trivial follow-up if it grows (#93 flagged it "only if status_log grows").

-- 1. Backfill: attribute orphan rows via the unambiguous user_jobs owner of the
--    same posting. Only postings owned by exactly one user are touched; on a
--    real DB there are no orphans (fresh installs always wrote user_id;
--    single-tenant DBs were backfilled in 20260614140000), so this is a no-op
--    safety net rather than a guess.
update public.status_log s
set user_id = uj.user_id
from (
    select job_posting_id, min(user_id) as user_id
    from public.user_jobs
    group by job_posting_id
    having count(distinct user_id) = 1
) uj
where s.user_id is null
  and uj.job_posting_id = s.posting_id;

-- 2. Enforce the invariant now that every row is attributed.
alter table public.status_log alter column user_id set not null;

-- 3. Per-user RLS read policy (mirrors "Users read their own analyses").
--    Zero effect on today's service-role code path; it's the backstop for the
--    not-yet-wired per-request user JWT client (#79). Writes stay service-role.
drop policy if exists "Users read their own status_log" on public.status_log;
create policy "Users read their own status_log" on public.status_log
  for select to authenticated
  using (((select auth.uid() as uid) = user_id));
