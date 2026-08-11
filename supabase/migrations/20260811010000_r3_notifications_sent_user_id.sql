-- R3 §2 (issue #557): repoint notifications_sent from the user_profiles.id
-- surrogate to the auth uid, killing the last cross-table dependency on that
-- surrogate.
--
-- notifications_sent was the only table keyed by `user_profiles.id` rather than
-- `auth.users.id`. That one exception costs a profile lookup on every path that
-- touches the table — the alert dedup claim, the SMS daily-rate count, the
-- erasure step, and the data export — and it is why account_deletion has a
-- bespoke step 4 (`_resolve_profile_id`) outside its `_USER_ID_TABLES` loop.
-- After this it is an ordinary `user_id` table like every other per-user table,
-- and matches the house FK convention:
--   FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE
-- (same as user_profiles, user_targets, llm_costs).
--
-- GATE — this is a REPOINT, not a backfill, and that is only true while the
-- table is empty. Notifications are config-gated off pending SMTP/Twilio
-- credentials, so nothing has ever written a row. Verified on prod immediately
-- before writing this migration:
--
--   SELECT count(*) FROM notifications_sent;   -> 0
--
-- If that ever returns non-zero, STOP: this migration drops the column outright
-- and would take the rows' identity with it. The replacement would be an
-- add-column + backfill-from-user_profiles + set-not-null sequence instead.
--
-- guarded-destructive: `user_profile_id` is dropped rather than backfilled
-- because the table is EMPTY on prod (0 rows, verified above) — there is no row
-- data to lose and nothing to snapshot. The column is NOT NULL, so the ADD
-- COLUMN ... NOT NULL below is likewise only valid on an empty table; it would
-- fail loudly rather than silently corrupt if that gate were ever wrong.
-- Dropping the column also drops the objects that depend on it — the
-- `notifications_sent_user_profile_job_channel_key` unique constraint, its
-- backing index, the `job_notification_sent_user_profile_id_fkey` FK to
-- user_profiles, and `idx_notifications_sent_user_channel_sent` — so each is
-- recreated below against `user_id`. RLS is enabled with no policy (service-role
-- only) and is unaffected by a column swap.

ALTER TABLE public.notifications_sent DROP COLUMN user_profile_id;

ALTER TABLE public.notifications_sent
    ADD COLUMN user_id uuid NOT NULL
    REFERENCES auth.users (id) ON DELETE CASCADE;

-- Re-create the dedup key that made the alert claim idempotent. This is the
-- constraint `notify._try_send_one` / `_try_send_sms` name in their upsert
-- `on_conflict`, so the column list must stay in this order.
ALTER TABLE public.notifications_sent
    ADD CONSTRAINT notifications_sent_user_job_channel_key
    UNIQUE (user_id, job_posting_id, channel);

-- Re-create the SMS daily-rate lookup index: `_sms_count_today` counts by
-- (user, channel) over a sent_at window.
CREATE INDEX idx_notifications_sent_user_channel_sent
    ON public.notifications_sent (user_id, channel, sent_at DESC);

COMMENT ON COLUMN public.notifications_sent.user_id IS
    'Auth uid (auth.users.id) the alert was sent to. R3 §2 (#557) replaced the '
    'user_profiles.id surrogate this table used to carry, so erasure and export '
    'key it like every other per-user table.';
