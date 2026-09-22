-- #1090: an append-only record of every activation the sweep gave up on.
--
-- In plain terms: when the safety-net sweep finds a target whose setup work
-- never finished, it resets the target and logs a warning. Logs age out and
-- the reset leaves no mark, so nobody can say afterwards how often deferred
-- work is abandoned — and that number is exactly what decides how much
-- architecture #1090 is worth.
--
-- Why a table rather than a column on `targets`: a column holds ONE value and
-- gets cleared when the user re-activates, so repeated reclaims of the same
-- target collapse into one and a target that recovers vanishes from the count
-- entirely. That measures "targets currently stuck and not yet revisited",
-- which is survivor-biased and cannot tell a rare defect from a frequent one
-- with quick recovery. Append-only rows answer the actual question.
--
-- It also keeps the failure-context contract intact: `targets.activation_error`
-- is documented and tested as non-null only while `activation_status = 'error'`,
-- and a reclaimed target is deliberately `idle`. Recording the event here
-- avoids overloading those fields for a non-error state.

CREATE TABLE IF NOT EXISTS public.activation_reclaims (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_id    uuid NOT NULL REFERENCES public.targets(id) ON DELETE CASCADE,
    -- The in-flight status the row was stuck in ('deriving' / 'polling'):
    -- which stage was abandoned is part of the question.
    from_status  text NOT NULL,
    -- How long it had been stuck when the sweep caught it, in hours, as
    -- configured at the time. Recorded so a later change to the window does
    -- not make older rows unreadable.
    stale_after_hours integer NOT NULL,
    reclaimed_at timestamptz NOT NULL DEFAULT now()
);

-- The only query this table exists to serve: how many, how recently, by stage.
CREATE INDEX IF NOT EXISTS idx_activation_reclaims_reclaimed_at
    ON public.activation_reclaims (reclaimed_at DESC);

COMMENT ON TABLE public.activation_reclaims IS
    'Append-only log of activations the stalled-activation sweep gave up on. '
    'Exists to size how often deferred work is abandoned before choosing an '
    'architecture for it (#1090). Operational; not user-facing.';

-- Operational table for the service-role client only, matching scheduler_runs.
ALTER TABLE public.activation_reclaims ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.activation_reclaims FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
