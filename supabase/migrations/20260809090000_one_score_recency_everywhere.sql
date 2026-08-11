-- #665: one score. Filter = sort = display, and that number is
-- ``scores.recency_score``.
--
-- OWNER DECISION (2026-08-08): a wyrdfold score means "good match AND still
-- fresh". Decay is not cosmetic ageing — it encodes *the probability the role is
-- still open*: "an old listing should have decayed in value because chances are
-- the company is no longer looking". So a stale high-fit posting genuinely is
-- worth less than a fresh mid-fit one, and the floor should say so. "Score 70+"
-- therefore legitimately means ">= 70 AFTER ageing", and a 79 that is three
-- weeks old dropping out of that filter is CORRECT, not a regression.
--
-- WHAT WAS WRONG: the list filtered the RAW score, but sorted and displayed the
-- DECAYED one. Same chip, three different numbers. Prod, before this change:
-- "Score 70+" returned rows rendering 69/67/62/61/60/58/56 — every one with a
-- raw score >= 70, so the floor was doing exactly what it was told while the
-- card showed something else entirely.
--
-- WHY THE STORED COLUMN, NOT A READ-TIME COMPUTATION:
-- ``recency.py``'s own design note prescribes it ("STORE the decayed score ...
-- rather than computing it at read time"). The objection that later justified
-- read-time decay — that the stored column "freezes for jobs the poller stops
-- re-touching" — is NO LONGER TRUE: ``scheduler.py`` ->
-- ``refresh_all_recency_scores`` now rewrites every live score row on a tick.
-- Measured on prod 2026-08-08:
--     pending  43,959 rows | 0 NULL | 100% within +/-1 of a fresh calc
--     graded    1,776 rows | 0 NULL |  98% within +/-1
-- The read-time transform had become a redundant second implementation, which
-- is precisely how it drifted away from the floor.
--
-- ``recency_score`` is also an identity write when the decay flag is off
-- (``compute_recency_score(..., enabled=False)`` returns ``score``), so keying
-- on it is correct in both modes and the ``p_recency_decay`` branch is no longer
-- needed for the un-weighted path.
--
-- The covering index that makes the new floor indexable ships in the sibling
-- migration 20260809085000 (built CONCURRENTLY on prod — `scores` is hot).
--
-- NULL-SAFETY is handled at the WRITE site, not here. ``recency_score`` was
-- nullable with no default, and a NULL makes ``recency_score >= N`` evaluate
-- NULL — so a row that missed the writer would silently VANISH from every
-- floored list. COALESCEing on read was the obvious patch and the wrong one: it
-- has to be repeated at every read site, and PostgREST cannot express it, so the
-- Python two-query floor would still drop the row and the two paths would
-- disagree (caught by the RPC-vs-Python equivalence matrix). The sibling
-- migration 20260809084000 makes the trigger fill it instead, so every reader —
-- SQL or PostgREST — sees a real number.
--
-- CUSTOM AXIS WEIGHTS are the one exception: ``recency_score`` derives from
-- ``score``, not from the weighted blend, so that path still decays at read
-- time (``v_gk``). 1 of 10 memberships today. The FLOOR still keys on
-- ``recency_score`` in every case — it is the indexable, canonical number, and
-- a weighted blend cannot be indexed.

-- ---------------------------------------------------------------------------
-- 1. pipeline_counts — the dashboard tiles floor on the same number.
-- ---------------------------------------------------------------------------
-- Keep in lockstep with ``_apply_score_floor`` and ``get_cross_target_jobs``;
-- ``tests/test_score_floor_sites_agree.py`` pins all three.
CREATE OR REPLACE FUNCTION public.pipeline_counts(
  p_target_ids uuid[],
  p_min_score integer,
  p_user_id uuid DEFAULT NULL::uuid
)
RETURNS TABLE(status text, count bigint)
LANGUAGE sql
STABLE
SET search_path TO 'public', 'pg_catalog'
AS $function$
  SELECT COALESCE(uj.status, 'new') AS status, COUNT(DISTINCT s.job_posting_id)
  FROM   public.scores s
  JOIN   public.jobs j ON j.id = s.job_posting_id
  JOIN   public.targets t ON t.id = s.target_id
  LEFT JOIN public.user_jobs uj
    ON uj.job_posting_id = j.id AND uj.user_id = p_user_id
  WHERE  s.target_id = ANY (p_target_ids)
    AND  s.excluded = false
    AND  j.archived_at IS NULL
    AND  j.purged_at IS NULL
    AND  j.is_us IS NOT FALSE
    AND  (t.role_family IS NULL OR j.role_family IS NULL OR j.role_family = t.role_family)
    AND  (p_min_score IS NULL
          OR s.axis_scores IS NULL
          OR s.recency_score >= p_min_score)
  GROUP BY COALESCE(uj.status, 'new');
$function$;

-- ---------------------------------------------------------------------------
-- 2. get_cross_target_jobs — floor, sort key, returned score, dedup tiebreak.
-- ---------------------------------------------------------------------------
-- Four coordinated changes, all so the ONE number is used consistently:
--   a. floor       : s.score >= $3            -> s.recency_score >= $3
--   b. sort key    : un-weighted case drops the inline now() decay expression
--                    and uses b.recency_score (indexable, and identical by
--                    construction). Weighted case keeps read-time decay.
--   c. returned    : `score` is now the SAME expression the sort uses, so the
--                    number shown is the number ranked and filtered on.
--                    `raw_score` still carries the undecayed fit.
--   d. tiebreak    : DISTINCT ON ... ORDER BY s.score DESC -> s.recency_score
--                    DESC, so the per-job representative is chosen by the same
--                    number too. ``_prefer_score_row`` in jobs.py mirrors this
--                    and MUST move with it.
CREATE OR REPLACE FUNCTION public.get_cross_target_jobs(
    p_target_ids uuid[],
    p_min_score integer DEFAULT 0,
    p_status text DEFAULT NULL::text,
    p_company text DEFAULT NULL::text,
    p_search text DEFAULT NULL::text,
    p_sort text DEFAULT 'score'::text,
    p_ascending boolean DEFAULT false,
    p_limit integer DEFAULT 20,
    p_offset integer DEFAULT 0,
    p_user_id uuid DEFAULT NULL::uuid,
    p_recency_decay boolean DEFAULT false,
    p_weights jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(id uuid, external_id text, source_id uuid, title text, company_name text, location text, city text, state text, country text, location_remote boolean, employment_type text, seniority text, metro text, is_remote boolean, absolute_url text, score integer, raw_score integer, score_breakdown jsonb, scoring_status text, logistics_filters jsonb, status text, salary_text text, salary_min numeric, salary_max numeric, salary_currency text, salary_period text, source_posted_at timestamp with time zone, cataloged_at timestamp with time zone, pending boolean)
LANGUAGE plpgsql
STABLE
SET search_path TO ''
AS $function$
DECLARE
    v_dir        text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_need_axis  boolean := (p_weights IS NOT NULL AND p_weights <> '{}'::jsonb);
    v_axis_col   text;
    v_gk         text;
    v_order      text;
    v_page_jobs  boolean;
    v_page_join  text;
    v_page_filt  text;
    v_sql        text;
BEGIN
    IF v_need_axis THEN
        -- Custom weights: recency_score is derived from `score`, not from the
        -- blend, so this path must still decay at read time.
        v_gk := 'public.wyrdfold_display_score(b.axis_scores, b.score, $9 -> b.target_id::text)';
        IF p_recency_decay THEN
            v_gk := v_gk || ' * GREATEST(0.3, 1.0 - GREATEST(0.0, COALESCE(EXTRACT(EPOCH FROM (now() - b.job_first_seen_at)) / 86400.0, 0.0) - 7.0) * 0.015)';
        END IF;
        v_axis_col := ', s.axis_scores';
    ELSE
        -- The stored column already IS score * decay (and an identity write
        -- when the decay flag is off), so no read-time arithmetic is needed —
        -- which is exactly what makes the floor indexable.
        v_gk       := 'b.recency_score';
        v_axis_col := '';
    END IF;

    v_page_jobs := (p_sort IN ('created_at', 'company_name', 'title'))
                   OR (p_company IS NOT NULL)
                   OR (p_search IS NOT NULL);

    IF p_sort = 'created_at' THEN
        v_order := format('jp.cataloged_at %1$s, b.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'company_name' THEN
        v_order := format('jp.company_name %1$s, b.job_posting_id %1$s', v_dir);
    ELSIF p_sort = 'title' THEN
        v_order := format('jp.title %1$s, b.job_posting_id %1$s', v_dir);
    ELSE
        v_order := format(
            'b.is_graded DESC, CASE WHEN b.is_graded THEN %1$s END %2$s, '
            || 'CASE WHEN NOT b.is_graded THEN COALESCE(b.job_first_seen_at, ''-infinity''::timestamptz) END %2$s, '
            || 'b.job_posting_id %2$s', v_gk, v_dir);
    END IF;

    IF v_page_jobs THEN
        v_page_join := 'INNER JOIN public.jobs jp ON jp.id = b.job_posting_id';
        v_page_filt := 'AND ($5 IS NULL OR jp.company_name = $5) '
                    || 'AND ($6 IS NULL OR jp.title ILIKE ''%'' || $6 || ''%'')';
    ELSE
        v_page_join := '';
        v_page_filt := 'AND ($5 IS NULL) AND ($6 IS NULL)';
    END IF;

    v_sql := format($q$
        WITH tgt AS MATERIALIZED (
            SELECT id, role_family FROM public.targets WHERE id = ANY ($2)
        ),
        uj_user AS MATERIALIZED (
            SELECT job_posting_id, status FROM public.user_jobs WHERE user_id = $1
        ),
        best AS (
            SELECT DISTINCT ON (s.job_posting_id)
                s.job_posting_id, s.target_id, s.score, s.recency_score, s.scoring_status,
                s.is_graded, s.job_role_family, s.job_first_seen_at%4$s
            FROM public.scores s
            WHERE s.target_id = ANY ($2)
                AND s.job_is_live
                AND s.excluded = FALSE
                AND ($3 IS NULL
                     OR $3 = 0
                     OR NOT s.is_graded
                     OR s.recency_score >= $3)
            ORDER BY s.job_posting_id, s.is_graded DESC, s.recency_score DESC
        ),
        page AS (
            SELECT b.*, COALESCE(uj.status, 'new')::text AS uj_status
            FROM best b
            INNER JOIN tgt t ON t.id = b.target_id
            %2$s
            LEFT JOIN uj_user uj
                ON uj.job_posting_id = b.job_posting_id
            WHERE (t.role_family IS NULL OR b.job_role_family IS NULL OR b.job_role_family = t.role_family)
                AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
                %3$s
            ORDER BY %1$s
            LIMIT $7 OFFSET $8
        )
        SELECT
            b.job_posting_id AS id, jp.external_id, jp.source_id, jp.title,
            jp.company_name, jp.location,
            jp.city, jp.state, jp.country, jp.location_remote,
            jp.employment_type, jp.seniority, jp.metro, jp.is_remote,
            jp.absolute_url,
            ROUND(%5$s)::integer AS score, b.score AS raw_score, s2.score_breakdown,
            b.scoring_status, s2.logistics_filters,
            b.uj_status AS status, jp.salary_text, jp.salary_min, jp.salary_max,
            jp.salary_currency, jp.salary_period, jp.source_posted_at,
            jp.cataloged_at, (NOT b.is_graded) AS pending
        FROM page b
        INNER JOIN public.jobs jp ON jp.id = b.job_posting_id
        LEFT JOIN LATERAL (
            SELECT s2.score_breakdown, s2.logistics_filters
            FROM public.scores s2
            WHERE s2.job_posting_id = b.job_posting_id AND s2.target_id = b.target_id
            LIMIT 1
        ) s2 ON TRUE
        ORDER BY %1$s
    $q$, v_order, v_page_join, v_page_filt, v_axis_col, v_gk);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset, p_weights;
END;
$function$;
