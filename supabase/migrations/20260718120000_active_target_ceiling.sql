-- SEC-M1 (audit 2026-07-18): absolute ceiling on active targets per user.
--
-- The per-tier active-target cap is enforced only in Python
-- (crud.enforce_active_limit). `user_targets` has a FOR ALL RLS policy and the
-- anon key ships in the browser, so a user can POST /rest/v1/user_targets with
-- {is_active:true} directly, ×N, bypassing the Python cap — and each active
-- target enrols into the poller (LLM spend), so the abuse is UNBOUNDED.
--
-- This is a DB **sanity ceiling**, NOT the per-plan cap (that stays in Python):
-- no user may hold more than a hard maximum of active targets, by any path.
-- The ceiling (25) sits far above any real plan/override (free 1 / starter 2 /
-- pro 5), so it never blocks a legitimate user — it only kills the runaway.
-- Raise via a migration if a legit need ever exceeds it.
--
-- Implemented as an AFTER ROW trigger (not BEFORE): a naive per-row BEFORE
-- count is bypassable by a single BULK insert (each new row sees the
-- pre-statement count). AFTER, the row that crosses the ceiling sees the true
-- running count and raises, rolling back the whole statement — bulk-safe. The
-- exception fires on the first over-limit row, so a huge bulk aborts early.

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

DROP TRIGGER IF EXISTS trg_enforce_active_target_ceiling ON public.user_targets;
CREATE TRIGGER trg_enforce_active_target_ceiling
    AFTER INSERT OR UPDATE ON public.user_targets
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_active_target_ceiling();
