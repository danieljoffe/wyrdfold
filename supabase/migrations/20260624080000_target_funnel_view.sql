-- target_funnel: operator-only analytics view — the per-target relevance funnel.
--
-- One row per target showing how candidate jobs collapse from
-- graded -> promising -> phase-2 -> score cutoffs, over LIVE jobs only
-- (archived_at IS NULL). This is the "ruler" for the relevance/discovery work
-- (issue #60): it turns "where is relevance lost for this target?" into a
-- single SELECT instead of an ad-hoc multi-CTE query.
--
-- Columns:
--   domain     - the target's first search keyword, a human label for the role family
--   graded     - live jobs that have a scores row for this target
--   promising  - of those, flagged promising by the LLM title triage
--   phase2     - of those, with a non-zero (phase-2) fit score
--   ge30/50/75 - live jobs scoring >= 30 / 50 / 75 for this target
--   max_score  - best fit score among this target's live jobs
--
-- Counts use count(j.id) so scores pointing at archived jobs don't inflate the
-- funnel; max_score is likewise restricted to live jobs.
--
-- This aggregates ACROSS ALL targets/tenants, so it is operator-only: SELECT is
-- revoked from the API roles (anon/authenticated) and granted only to
-- service_role. security_invoker=true keeps it from running with elevated
-- (definer) rights. It is a plain (non-materialized) view: zero stored rows, it
-- just re-runs the query on read.

create or replace view public.target_funnel
with (security_invoker = true) as
select
  t.id as target_id,
  t.is_active,
  (
    select kw.value
    from jsonb_array_elements_text(t.search_keywords) with ordinality kw(value, ord)
    order by kw.ord
    limit 1
  ) as domain,
  count(j.id) as graded,
  count(j.id) filter (where s.promising) as promising,
  count(j.id) filter (where s.score > 0) as phase2,
  count(j.id) filter (where s.score >= 30) as ge30,
  count(j.id) filter (where s.score >= 50) as ge50,
  count(j.id) filter (where s.score >= 75) as ge75,
  coalesce(max(s.score) filter (where j.id is not null), 0) as max_score
from public.targets t
left join public.scores s on s.target_id = t.id
left join public.jobs j on j.id = s.job_posting_id and j.archived_at is null
group by t.id, t.is_active, t.search_keywords;

revoke all on public.target_funnel from anon, authenticated;
grant select on public.target_funnel to service_role;

comment on view public.target_funnel is
  'Operator-only per-target relevance funnel over live jobs (issue #60). '
  'Cross-tenant aggregate; SELECT granted to service_role only.';
