-- #1088: the scheduler ledger records whether a job SUCCEEDED, not just that
-- it was attempted.
--
-- In plain terms: this table is how the app answers "has this job run
-- recently?" after a deploy. It only ever recorded that a job STARTED. So a
-- job that failed on every single attempt still looked perfectly healthy
-- here, and the boot-time catch-up — the one mechanism that exists to notice
-- a starved job — could never fire for it. That is exactly how the recency
-- sweep went unnoticed for over two weeks while failing twice a day, and all
-- six jobs that use this table share the shape.
--
-- Why `last_run_at` stays as it is: it is the storm guard. The original note
-- on this table is right that stamping the attempt keeps a crash-looping job
-- from re-firing on every boot, and this app deploys often. So the attempt
-- marker keeps its meaning and its job, and success is recorded ALONGSIDE it
-- rather than replacing it. The catch-up now needs both: it asks "has this
-- succeeded lately?" to decide whether the job is starved, and "when did we
-- last try?" to decide whether trying again right now would be a storm.
--
-- Nullable with no backfill on purpose. A NULL means "we have never recorded
-- a success for this job", which is the honest state for every row the moment
-- this ships, and it is indistinguishable from a job that has genuinely never
-- succeeded — which several of them may not have. The first successful run of
-- each job fills it in.

ALTER TABLE public.scheduler_runs
    ADD COLUMN IF NOT EXISTS last_success_at timestamptz;

COMMENT ON COLUMN public.scheduler_runs.last_run_at IS
    'When the job last STARTED. The storm guard: a crash-looping job waits '
    'rather than re-firing on every boot (#327).';

COMMENT ON COLUMN public.scheduler_runs.last_success_at IS
    'When the job last COMPLETED its work. NULL means never recorded. Read by '
    'the boot-time catch-up to tell a starved job from a healthy one — an '
    'attempt marker alone cannot (#1088).';

COMMENT ON TABLE public.scheduler_runs IS
    'Per-job scheduler ledger: last attempt AND last success, read by '
    'boot-time catch-ups so deploys stop resetting long interval timers '
    '(#327) and so a job that fails every tick stops looking healthy (#1088).';

NOTIFY pgrst, 'reload schema';
