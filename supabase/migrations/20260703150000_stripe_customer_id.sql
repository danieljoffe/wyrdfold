-- Phase 3 slice 3 — Stripe billing. One column: the user's Stripe
-- Customer id, written by the API when a checkout session is first
-- created (and re-asserted by the webhook), read to resolve webhook
-- events back to a user and to open the billing portal. Unique where
-- present: two users must never share a Stripe customer. No RLS change —
-- user_profiles policies already scope rows to their owner, and the
-- column is only ever written by the service-role backend.

ALTER TABLE public.user_profiles
    ADD COLUMN IF NOT EXISTS stripe_customer_id text;

CREATE UNIQUE INDEX IF NOT EXISTS user_profiles_stripe_customer_id_key
    ON public.user_profiles (stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;
