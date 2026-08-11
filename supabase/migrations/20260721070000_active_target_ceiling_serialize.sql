-- Hardening review 2026-07-21 (DB-F1): serialize the active-target ceiling
-- trigger so concurrent single-row inserts can't slip past it.
--
-- 20260718120000 added an AFTER-ROW trigger that counts a user's active
-- user_targets and raises past a hard ceiling (25). That count runs under
-- READ COMMITTED with no serialization: two concurrent single-row inserts for
-- the same user each take their own statement snapshot and are mutually
-- invisible, so each counts below the ceiling and commits. Firing ~N parallel
-- `POST /rest/v1/user_targets {is_active:true}` requests (the anon key ships in
-- the browser) therefore lets a user hold far more than 25 active targets —
-- every one of which enrols into the poller (LLM spend) — which is exactly the
-- unbounded abuse 20260718120000 set out to cap.
--
-- Fix: take a per-user transaction-scoped advisory lock before counting. Any
-- other transaction inserting for the same user blocks on the lock, then
-- re-snapshots on its next statement and sees the committed rows — so the count
-- is accurate and the ceiling holds under concurrency. This mirrors the
-- FOR UPDATE serialization the profile-write RPCs already use; there is no
-- single row to lock here (we count across rows), so an advisory lock keyed by
-- user_id is the right primitive. The lock is released automatically at
-- commit/rollback (xact-scoped).

CREATE OR REPLACE FUNCTION public.enforce_active_target_ceiling()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_ceiling constant int := 25;
    v_active int;
BEGIN
    -- Only a row that is active can push the count over.
    IF NEW.is_active IS NOT TRUE THEN
        RETURN NULL;  -- AFTER trigger: return value ignored
    END IF;
    -- Serialize concurrent inserts/activations for THIS user so the count below
    -- sees every competing transaction's committed rows (they queue on the lock
    -- and re-snapshot). Without this the count races and the ceiling leaks.
    PERFORM pg_advisory_xact_lock(hashtext('user_targets_ceiling:' || NEW.user_id::text));
    SELECT count(*) INTO v_active
    FROM public.user_targets
    WHERE user_id = NEW.user_id AND is_active = true;
    IF v_active > v_ceiling THEN
        RAISE EXCEPTION
            'active target ceiling (%) exceeded for user %', v_ceiling, NEW.user_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;

-- A SECURITY DEFINER function gets a PUBLIC EXECUTE grant by default; a trigger
-- function is only ever invoked by the trigger, never called directly, so
-- revoke it (the privilege-invariants test enforces: no DEFINER fn is
-- anon/authenticated-executable).
REVOKE ALL ON FUNCTION public.enforce_active_target_ceiling() FROM PUBLIC, anon, authenticated;

-- Trigger definition is unchanged (CREATE OR REPLACE FUNCTION keeps it bound);
-- re-declared here only for readers.
-- Trigger: trg_enforce_active_target_ceiling AFTER INSERT OR UPDATE ON user_targets.
