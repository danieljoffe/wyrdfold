-- Phase 0 (deployment-modes): seed the reserved SYSTEM principal.
--
-- The data model is moving to a single invariant — every per-user row has a
-- NON-NULL owner (see docs/plan-wyrdfold-deployment-modes.md). Rows authored by
-- the system itself (the cron poller's job gradings in `analyses`, the
-- `llm_costs` ledger, batch output) currently carry `user_id IS NULL` — the
-- legacy single-tenant marker that api-key callers thread through
-- `get_current_user_id_optional`. They need a real owner instead.
--
-- This seeds that owner: a reserved `auth.users` row with a fixed, well-known id
-- (app/constants.py SYSTEM_USER_ID). It is a REAL auth.users row — not a bare
-- sentinel — so that the `user_id -> auth.users(id) ON DELETE CASCADE` FK added
-- later (Phase 0 step 4) holds for system-owned rows too. That FK is
-- defense-in-depth UNDER the app's #29 erasure flow (which also purges Storage +
-- external state a DB cascade can't), a backstop against orphaned rows — not a
-- replacement for it.
--
-- The principal can NEVER authenticate: no `encrypted_password` and no
-- `identities` row (so neither password nor OAuth login is possible), and its
-- email domain is non-routable (`.internal`, so no magic link can be delivered).
-- Its id is also not a valid v4 UUID (version nibble 0, not 4), so GoTrue can
-- never mint a colliding real user → no human's `auth.uid()` equals it → RLS
-- (`auth.uid() = user_id`) keeps every SYSTEM-owned row invisible to every user.
--
-- Idempotent (ON CONFLICT DO NOTHING) so re-applying across environments — and a
-- fresh self-host bring-up — is safe.

INSERT INTO auth.users (
    id,
    aud,
    role,
    email,
    email_confirmed_at,
    created_at,
    updated_at,
    raw_app_meta_data,
    raw_user_meta_data
)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'authenticated',
    'authenticated',
    'system@wyrdfold.internal',
    now(),
    now(),
    now(),
    '{"provider":"system","providers":["system"]}'::jsonb,
    '{"system":true,"label":"Wyrdfold system principal"}'::jsonb
)
ON CONFLICT (id) DO NOTHING;
