-- Phase 3 slice 4 — waitlist→invite funnel. One column: when an operator
-- converted this signup into a beta invite (NULL = still pending). Powers
-- the operator's "who's left to invite" view and keeps the funnel state
-- inside the existing table instead of a parallel ledger. No RLS change:
-- waitlist_signups stays deny-all for anon/authenticated (service-role
-- writes only, 20260623130000).

ALTER TABLE public.waitlist_signups
    ADD COLUMN IF NOT EXISTS invited_at timestamp with time zone;
