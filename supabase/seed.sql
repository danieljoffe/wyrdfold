-- Local development seed.
--
-- Wired up by supabase/config.toml ([db.seed] sql_paths), so it runs on
-- `supabase start` against a fresh volume and on every `supabase db reset`.
-- Before this file existed the config pointed at it anyway and every reset
-- printed `WARN: no files matched pattern: supabase/seed.sql`, leaving a
-- correctly-installed checkout with 0 jobs and 0 targets — a working app with
-- nothing in it, which reads as broken.
--
-- WHAT THIS GIVES YOU: enough of a catalog that `/search` returns results
-- immediately, with no API keys, no poller run and no LLM spend. That is the
-- one surface a brand-new checkout can exercise while signed out.
--
-- WHAT IT DELIBERATELY DOES NOT DO: create accounts, targets or matches.
-- Those hang off a real auth user, and auth users come from GoTrue via the
-- magic-link flow rather than from SQL — seeding `auth.users` by hand desyncs
-- identities and refresh tokens and breaks sign-in in confusing ways. Sign up
-- through the app (locally, the magic-link email lands in Mailpit on :54324)
-- and the onboarding wizard builds the rest. README -> "Local development".
--
-- EVERY COMPANY AND POSTING BELOW IS FICTIONAL. This file is committed to a
-- public repository: it must never carry scraped listings, real candidate or
-- employer data, or live URLs that imply a relationship. `example.com` is
-- reserved by RFC 2606 precisely for this.
--
-- IDEMPOTENT on purpose: `db reset` replays it, and a developer may also run
-- it by hand against a database that already has these rows. Fixed UUIDs plus
-- ON CONFLICT DO NOTHING make a re-run a no-op rather than a duplicate-key
-- failure. Fixed UUIDs also let the jobs below reference their source without
-- a lookup.
--
-- Rows must satisfy the public-search visibility gate or they will not show up:
-- archived_at IS NULL, purged_at IS NULL, is_us IS NOT FALSE
-- (app/services/job_search.py). They are set explicitly here so the reason is
-- visible rather than inherited silently from column defaults.

begin;

-- enabled = FALSE, deliberately. These board tokens are fictional, so a
-- scheduler that picked them up would issue doomed requests to Greenhouse,
-- Lever and Ashby, rack up consecutive_failures, and — because a poll that
-- finds none of a source's existing jobs archives them — quietly delete the
-- starter catalog it is meant to provide. The poller selects on
-- `enabled = true` (poller.py), so leaving them disabled keeps them
-- unreachable by discovery on ANY instance, including a hosted one where
-- someone later turns the scheduler on.
--
-- Search is unaffected: it joins `sources(domain)` by foreign key for the
-- company domain and never filters on `enabled` (job_search.py).
insert into public.sources (id, board_token, company_name, provider, domain, enabled)
values
  ('5eed0000-0000-4000-a000-000000000001', 'seed-northwind',  'Northwind Systems',  'greenhouse', 'example.com', false),
  ('5eed0000-0000-4000-a000-000000000002', 'seed-lumenworks', 'Lumenworks',         'greenhouse', 'example.com', false),
  ('5eed0000-0000-4000-a000-000000000003', 'seed-atlasgrove', 'Atlas Grove Health', 'lever',      'example.com', false),
  ('5eed0000-0000-4000-a000-000000000004', 'seed-harborline', 'Harborline Freight', 'ashby',      'example.com', false)
on conflict (board_token) do nothing;

-- A spread rather than sixteen near-identical rows: remote and on-site, with
-- and without salary, several seniorities and two non-engineering families, so
-- filtering and sorting have something to bite on. source_posted_at is relative to
-- now() so the recency-biased ranker behaves like it does against real data
-- however long after cloning this runs.
insert into public.jobs (
  external_id, source_id, title, company_name, location, city, state, country,
  is_remote, location_remote, is_us, employment_type, salary_min, salary_max,
  salary_currency, salary_period, absolute_url, description_html, source_posted_at
)
values
  ('seed-001', '5eed0000-0000-4000-a000-000000000001', 'Senior Backend Engineer',            'Northwind Systems',  'Remote, US',       null,            null, 'US', true, true,  true, 'full_time', 165000, 205000, 'USD', 'yearly', 'https://example.com/jobs/seed-001', '<p>Own the ingestion services behind our reporting platform. Python, Postgres, and a lot of queue-shaped problems.</p>', now() - interval '1 day'),
  ('seed-002', '5eed0000-0000-4000-a000-000000000001', 'Staff Platform Engineer',            'Northwind Systems',  'Seattle, WA',      'Seattle',       'WA', 'US', false, false, true, 'full_time', 190000, 240000, 'USD', 'yearly', 'https://example.com/jobs/seed-002', '<p>Set the direction for build, deploy and observability across a dozen services.</p>', now() - interval '2 days'),
  ('seed-003', '5eed0000-0000-4000-a000-000000000001', 'Engineering Manager, Data',          'Northwind Systems',  'Remote, US',       null,            null, 'US', true, true,  true, 'full_time', null,   null,   null,  null,     'https://example.com/jobs/seed-003', '<p>Lead a team of six working on the warehouse and the pipelines that feed it.</p>', now() - interval '6 days'),
  ('seed-004', '5eed0000-0000-4000-a000-000000000002', 'Frontend Engineer',                  'Lumenworks',         'Remote, US',       null,            null, 'US', true, true,  true, 'full_time', 130000, 160000, 'USD', 'yearly', 'https://example.com/jobs/seed-004', '<p>React and TypeScript, design-system work, and a product team that ships weekly.</p>', now() - interval '3 hours'),
  ('seed-005', '5eed0000-0000-4000-a000-000000000002', 'Senior Frontend Engineer',           'Lumenworks',         'Austin, TX',       'Austin',        'TX', 'US', false, false, true, 'full_time', 155000, 195000, 'USD', 'yearly', 'https://example.com/jobs/seed-005', '<p>Own the editor surface end to end — performance, accessibility and the plugin API.</p>', now() - interval '4 days'),
  ('seed-006', '5eed0000-0000-4000-a000-000000000002', 'Product Designer',                   'Lumenworks',         'Remote, US',       null,            null, 'US', true, true,  true, 'full_time', 120000, 150000, 'USD', 'yearly', 'https://example.com/jobs/seed-006', '<p>Shape the end-to-end experience for a small, opinionated product team.</p>', now() - interval '8 days'),
  ('seed-007', '5eed0000-0000-4000-a000-000000000002', 'Junior Software Engineer',           'Lumenworks',         'Austin, TX',       'Austin',        'TX', 'US', false, false, true, 'full_time', 95000,  115000, 'USD', 'yearly', 'https://example.com/jobs/seed-007', '<p>A first engineering role with real mentorship and real ownership.</p>', now() - interval '12 days'),
  ('seed-008', '5eed0000-0000-4000-a000-000000000003', 'Data Engineer',                      'Atlas Grove Health', 'Boston, MA',       'Boston',        'MA', 'US', false, false, true, 'full_time', 140000, 175000, 'USD', 'yearly', 'https://example.com/jobs/seed-008', '<p>Build the clinical data pipelines our analytics and reporting run on.</p>', now() - interval '5 days'),
  ('seed-009', '5eed0000-0000-4000-a000-000000000003', 'Senior Data Scientist',              'Atlas Grove Health', 'Remote, US',       null,            null, 'US', true, true,  true, 'full_time', 170000, 210000, 'USD', 'yearly', 'https://example.com/jobs/seed-009', '<p>Outcomes modelling on a large longitudinal dataset, working beside clinicians.</p>', now() - interval '9 days'),
  ('seed-010', '5eed0000-0000-4000-a000-000000000003', 'Site Reliability Engineer',          'Atlas Grove Health', 'Boston, MA',       'Boston',        'MA', 'US', false, false, true, 'full_time', null,   null,   null,  null,     'https://example.com/jobs/seed-010', '<p>Keep a regulated, high-availability platform boring. On-call is shared and humane.</p>', now() - interval '14 days'),
  ('seed-011', '5eed0000-0000-4000-a000-000000000003', 'Clinical Operations Analyst',        'Atlas Grove Health', 'Boston, MA',       'Boston',        'MA', 'US', false, false, true, 'full_time', 85000,  105000, 'USD', 'yearly', 'https://example.com/jobs/seed-011', '<p>Turn messy operational data into decisions the care teams can act on.</p>', now() - interval '18 days'),
  ('seed-012', '5eed0000-0000-4000-a000-000000000004', 'Backend Engineer, Logistics',        'Harborline Freight', 'Chicago, IL',      'Chicago',       'IL', 'US', false, false, true, 'full_time', 135000, 170000, 'USD', 'yearly', 'https://example.com/jobs/seed-012', '<p>Routing, scheduling and the constraint solver underneath both.</p>', now() - interval '7 days'),
  ('seed-013', '5eed0000-0000-4000-a000-000000000004', 'Principal Software Engineer',        'Harborline Freight', 'Remote, US',       null,            null, 'US', true, true,  true, 'full_time', 200000, 260000, 'USD', 'yearly', 'https://example.com/jobs/seed-013', '<p>The most senior IC seat on the platform. Deep systems work, wide blast radius.</p>', now() - interval '11 days'),
  ('seed-014', '5eed0000-0000-4000-a000-000000000004', 'DevOps Engineer',                    'Harborline Freight', 'Denver, CO',       'Denver',        'CO', 'US', false, false, true, 'contract',  null,   null,   null,  null,     'https://example.com/jobs/seed-014', '<p>Six-month contract to migrate a legacy deployment pipeline onto containers.</p>', now() - interval '16 days'),
  ('seed-015', '5eed0000-0000-4000-a000-000000000004', 'Technical Program Manager',          'Harborline Freight', 'Chicago, IL',      'Chicago',       'IL', 'US', false, false, true, 'full_time', 145000, 180000, 'USD', 'yearly', 'https://example.com/jobs/seed-015', '<p>Hold three engineering teams to one roadmap without becoming a bottleneck.</p>', now() - interval '21 days'),
  ('seed-016', '5eed0000-0000-4000-a000-000000000001', 'Software Engineer, Internal Tools',  'Northwind Systems',  'Remote, US',       null,            null, 'US', true, true,  true, 'part_time', 70000,  90000,  'USD', 'yearly', 'https://example.com/jobs/seed-016', '<p>Part-time role building the tooling the rest of engineering leans on daily.</p>', now() - interval '25 days')
on conflict (source_id, external_id) do nothing;

commit;
