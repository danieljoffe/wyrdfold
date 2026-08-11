-- R3 §3 (issue #557) + issue #649: make a failed activation diagnosable and a
-- stalled one recoverable.
--
-- WHAT THIS DELIBERATELY DOES NOT DO: it does not converge `idle` into `ready`.
-- #557 §3 describes them as "two different terminal states by path", but the
-- code says otherwise — `idle` means "derived, AWAITING activation" and is the
-- signal the frontend uses to fire POST /targets/{id}/activate (JobsList.tsx;
-- targetFlows.ts "kickoff surfaces as a still-idle target the user can
-- re-activate from"). Collapsing it into `ready` would remove the only
-- auto-activation trigger and leave new targets unpolled — the exact bug
-- TargetSuggestions.tsx records as already fixed once. The real lifecycle is
-- linear: deriving -> idle -> polling -> ready, with error as the failure sink.
--
-- 1. FAILURE CONTEXT (#649). `activation_status = 'error'` was terminal AND
--    silent: the two writers (no OptimizedDoc for the user vs. a bare `except`
--    around the whole pipeline) are wildly different — one is user-actionable
--    ("add your experience first"), the other a transient backend blip — and
--    neither persisted a reason, so the UI showed a red card and an operator
--    had nothing to diagnose from.

ALTER TABLE public.targets ADD COLUMN IF NOT EXISTS activation_error text;
ALTER TABLE public.targets ADD COLUMN IF NOT EXISTS activation_failed_at timestamptz;

COMMENT ON COLUMN public.targets.activation_error IS
    'Why the last activation attempt failed, as a short stable reason code '
    '(see app/services/targets/activation.py ActivationError). NULL once an '
    'activation succeeds — the pipeline clears it on reaching ready, which is '
    'what makes re-activation a real retry path (#649).';
COMMENT ON COLUMN public.targets.activation_failed_at IS
    'When the last activation attempt failed. Cleared alongside '
    'activation_error on a successful activation (#649).';

-- 2. HEAL THE STALLED ROWS (#557 §3 "backfill/heal existing stale prod rows").
--    `deriving` and `polling` are IN-FLIGHT states: something is supposed to be
--    working. Nothing ever swept them, so a crashed/detached task strands a
--    target forever — prod has one sitting in `polling` since 2026-07-14,
--    27 days at the time of writing, with a real follower.
--
--    Converge to `idle`, NOT to `error`. `idle` is the re-activatable state:
--    the frontend fires /activate on seeing it, and `_activate_pipeline`
--    re-derives when the profile is missing (`needs_derive`) and otherwise
--    goes straight to polling. So a swept row heals itself on the user's next
--    visit instead of showing them a red card for a backend stall.
--
--    The 6-hour floor is deliberately far longer than any real activation
--    (the derive path times out at 60s; polling a target's sources is minutes)
--    so this cannot catch a live pipeline. Even if it did, the failure mode is
--    a benign re-activation, not data loss.
UPDATE public.targets
SET activation_status = 'idle',
    updated_at = now()
WHERE activation_status IN ('deriving', 'polling')
  AND updated_at < now() - interval '6 hours';
