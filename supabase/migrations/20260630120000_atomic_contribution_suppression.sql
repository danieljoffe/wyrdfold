-- audit #29 (round-2 low) — lost-update race in the contribution-vote
-- suppression tally.
--
-- `votes.recompute_suppression` did a read-modify-write in Python: tally every
-- vote for a reference_jd, read the current `suppressed` flag, and write the new
-- one if it changed. With no lock, two concurrent recomputes (two users voting
-- on the SAME shared contribution at once) can interleave read/write so a stale
-- tally clobbers a fresh one — silently un/re-suppressing a contribution EVERY
-- user of that shared target then sees merged (or not) into their matching.
--
-- Fix: move the whole tally→compare→write into one SECURITY DEFINER function
-- that first takes a `FOR UPDATE` row lock on the contribution. Concurrent
-- recomputes on the same reference_jd serialize on that lock — the second
-- caller blocks until the first commits, then re-reads the flag and re-tallies
-- the (now fully committed) votes, so it can never write from a stale snapshot.
-- Different contributions lock different rows, so there is no cross-contribution
-- serialization.
--
-- Grants follow the locked-down convention (20260621150000 / 20260629120000):
-- owner postgres, PUBLIC/anon/authenticated revoked, EXECUTE to service_role
-- only — the backend calls it exclusively on the service-role client (the tally
-- must read every user's vote, which RLS hides from any single caller).
-- Non-destructive (CREATE OR REPLACE + grants only).

CREATE OR REPLACE FUNCTION public.recompute_contribution_suppression(
    p_reference_jd_id uuid,
    p_quorum int
) RETURNS TABLE(suppressed boolean, changed boolean)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path TO 'public', 'pg_catalog'
AS $$
DECLARE
    v_cur boolean;
    v_net_down int;
    v_new boolean;
BEGIN
    -- Serialize concurrent recomputes on this contribution (see header).
    SELECT r.suppressed INTO v_cur
    FROM public.reference_jds r
    WHERE r.id = p_reference_jd_id
    FOR UPDATE;

    IF NOT FOUND THEN
        -- Contribution deleted between the vote and the recompute — nothing to
        -- reconcile. Mirrors the Python guard's "no row -> (False, False)".
        suppressed := false;
        changed := false;
        RETURN NEXT;
        RETURN;
    END IF;

    -- NET down-votes = down(-1) minus up(+1) = -SUM(value); no votes -> 0.
    SELECT COALESCE(-SUM(v.value), 0) INTO v_net_down
    FROM public.contribution_votes v
    WHERE v.reference_jd_id = p_reference_jd_id;

    v_new := v_net_down >= p_quorum;
    v_cur := COALESCE(v_cur, false);

    IF v_new IS DISTINCT FROM v_cur THEN
        UPDATE public.reference_jds SET suppressed = v_new WHERE id = p_reference_jd_id;
        suppressed := v_new;
        changed := true;
    ELSE
        suppressed := v_new;
        changed := false;
    END IF;
    RETURN NEXT;
END;
$$;

ALTER FUNCTION public.recompute_contribution_suppression(uuid, int) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.recompute_contribution_suppression(uuid, int)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.recompute_contribution_suppression(uuid, int)
    TO service_role;
