-- R2 PR-c (schema audit Groups B/C/D — docs/decisions.md 2026-07-30 entries
-- "The provider posted-date was captured all along" + "names must tell the
-- truth"): the two-timestamp model + the source_registrations rename, plus
-- the single re-issue of both list RPCs (which also serves PR-a's tag
-- columns).
--
--   cataloged_at     (rename of jobs.created_at)  — when WE ingested it.
--   source_posted_at (rename of jobs.greenhouse_updated_at) — the PROVIDER's
--                    posted/created date (Lever createdAt, Ashby publishedAt,
--                    SmartRecruiters releasedDate, Workday postedOn, JSON-LD
--                    datePosted; Greenhouse updated_at as its API's best).
--   first_seen_at    DROPPED — byte-identical duplicate of created_at
--                    (asserted below before the drop).
--
-- Recency semantics change deliberately: scores.job_first_seen_at (column
-- name unchanged — a scores-side rename would widen this further) is now fed
-- COALESCE(source_posted_at, cataloged_at), so freshness reflects the
-- provider's date where we have one (~98% of rows) instead of our discovery
-- time, which overstated freshness for late-discovered boards.
--
-- NOT backward-compatible (renames) — ships in the R2 tight-cutover window.

-- ---- 0. Safety assertion, then the destructive bit ------------------------
-- guarded-destructive: first_seen_at is provably redundant — equal to
-- created_at on every row (both DEFAULT now(), never app-written, upserts
-- touch neither; audit doc Group C). The DO block makes the migration REFUSE
-- to run if that invariant ever stopped holding, so no snapshot is needed —
-- the surviving column IS the snapshot.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.jobs WHERE first_seen_at IS DISTINCT FROM created_at) THEN
        RAISE EXCEPTION 'first_seen_at diverged from created_at — do not drop; investigate';
    END IF;
END $$;

-- The jobs->scores denorm trigger's WHEN clause references first_seen_at, so
-- it must go before the column can. Re-created below with the new feed.
DROP TRIGGER IF EXISTS jobs_sync_scores_denorm_au ON public.jobs;

ALTER TABLE public.jobs DROP COLUMN IF EXISTS first_seen_at;
ALTER TABLE public.jobs RENAME COLUMN created_at TO cataloged_at;
ALTER TABLE public.jobs RENAME COLUMN greenhouse_updated_at TO source_posted_at;

COMMENT ON COLUMN public.jobs.cataloged_at IS
  'When the listing entered OUR catalog (row insert). Renamed from created_at '
  '(and absorbed the byte-identical first_seen_at) — R2, 2026-07-31.';
COMMENT ON COLUMN public.jobs.source_posted_at IS
  'The PROVIDER''s posted/created/updated date, normalized (see '
  'normalize_posted_at): Lever createdAt / Ashby publishedAt / '
  'SmartRecruiters releasedDate / Workday postedOn / JSON-LD datePosted / '
  'Greenhouse updated_at. NULL when the provider gave none (manual adds write '
  'NULL as of R2). Renamed from the misnomer greenhouse_updated_at.';

ALTER INDEX IF EXISTS idx_job_postings_created_at RENAME TO idx_jobs_cataloged_at;

-- ---- 1. source_ownerships -> source_registrations --------------------------
-- It is a from-url registration/abuse-cap ledger, not a sponsorship table
-- (payer logic reads user_targets). Constraint renamed for greppability;
-- the RLS policy follows the table automatically but is renamed to match.
ALTER TABLE public.source_ownerships RENAME TO source_registrations;
ALTER TABLE public.source_registrations
  RENAME CONSTRAINT source_ownerships_pkey TO source_registrations_pkey;
ALTER POLICY "Users read their own source_ownerships" ON public.source_registrations
  RENAME TO "Users read their own source_registrations";

-- register_source_from_url: body re-issued from the LIVE definition with only
-- the table name changed (grants/owner untouched by CREATE OR REPLACE).
CREATE OR REPLACE FUNCTION public.register_source_from_url(p_user_id uuid, p_provider text, p_board_token text, p_company_name text, p_cap integer)
 RETURNS text
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare
    v_source_id uuid;
    v_count int;
    v_inserted_source boolean := false;
begin
    select id into v_source_id from public.sources where board_token = p_board_token;

    -- Idempotent: re-pasting a board the caller already registered is a no-op,
    -- even if they're at the cap (they're not adding anything new).
    if v_source_id is not null and exists (
        select 1 from public.source_registrations
        where user_id = p_user_id and source_id = v_source_id
    ) then
        return 'already_owned';
    end if;

    -- Soft cap: bounds distinct boards a single user can register. Not locked
    -- against a same-user race (two concurrent registrations could both pass at
    -- count = cap-1), which is fine for an abuse control with a fuzzy ceiling.
    select count(*) into v_count from public.source_registrations where user_id = p_user_id;
    if v_count >= p_cap then
        return 'cap_reached';
    end if;

    if v_source_id is null then
        insert into public.sources (provider, board_token, company_name, enabled)
        values (p_provider, p_board_token, p_company_name, true)
        on conflict (board_token) do nothing
        returning id into v_source_id;
        if v_source_id is not null then
            v_inserted_source := true;
        else
            -- Lost the insert race — the row the other txn created is now visible.
            select id into v_source_id from public.sources where board_token = p_board_token;
        end if;
    end if;

    insert into public.source_registrations (user_id, source_id)
    values (p_user_id, v_source_id)
    on conflict (user_id, source_id) do nothing;

    if v_inserted_source then
        return 'registered';
    end if;
    return 'linked';
end;
$function$;

-- ---- 2. Denorm feed: job_first_seen_at now carries the posted-date ---------
CREATE OR REPLACE FUNCTION public.jobs_sync_scores_denorm()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
BEGIN
    UPDATE public.scores s SET
        job_is_live = (NEW.archived_at IS NULL AND NEW.purged_at IS NULL AND NEW.is_us IS NOT FALSE),
        job_role_family = NEW.role_family,
        job_first_seen_at = COALESCE(NEW.source_posted_at, NEW.cataloged_at)
    WHERE s.job_posting_id = NEW.id
      AND (s.job_is_live IS DISTINCT FROM (NEW.archived_at IS NULL AND NEW.purged_at IS NULL AND NEW.is_us IS NOT FALSE)
           OR s.job_role_family IS DISTINCT FROM NEW.role_family
           OR s.job_first_seen_at IS DISTINCT FROM COALESCE(NEW.source_posted_at, NEW.cataloged_at));
    RETURN NULL;
END;
$function$;

CREATE TRIGGER jobs_sync_scores_denorm_au
AFTER UPDATE ON public.jobs
FOR EACH ROW
WHEN (old.archived_at IS DISTINCT FROM new.archived_at
      OR old.purged_at IS DISTINCT FROM new.purged_at
      OR old.is_us IS DISTINCT FROM new.is_us
      OR old.role_family IS DISTINCT FROM new.role_family
      OR old.source_posted_at IS DISTINCT FROM new.source_posted_at)
EXECUTE FUNCTION public.jobs_sync_scores_denorm();

CREATE OR REPLACE FUNCTION public.scores_sync_denorm()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
BEGIN
    -- Gradedness is a same-row property (matches the RPC's axis_scores IS NOT NULL).
    NEW.is_graded := (NEW.axis_scores IS NOT NULL);
    -- Populate the jobs-derived columns from the parent job. On INSERT, take a
    -- FOR SHARE lock so a concurrent jobs archival/is_us-flip can't leave this
    -- row's job_is_live stale (DB-F6). Plain re-scores / recency sweeps skip the
    -- lookup: the job's liveness/family/posted-date don't change under a scores
    -- UPDATE (the jobs trigger owns those).
    IF TG_OP = 'INSERT' THEN
        SELECT (jp.archived_at IS NULL AND jp.purged_at IS NULL AND jp.is_us IS NOT FALSE),
               jp.role_family, COALESCE(jp.source_posted_at, jp.cataloged_at)
          INTO NEW.job_is_live, NEW.job_role_family, NEW.job_first_seen_at
          FROM public.jobs jp
         WHERE jp.id = NEW.job_posting_id
         FOR SHARE;
    ELSIF NEW.job_is_live IS NULL THEN
        -- Legacy pre-backfill recovery: no lock (see header — avoids inverting
        -- the lock order against the jobs->scores fan-out).
        SELECT (jp.archived_at IS NULL AND jp.purged_at IS NULL AND jp.is_us IS NOT FALSE),
               jp.role_family, COALESCE(jp.source_posted_at, jp.cataloged_at)
          INTO NEW.job_is_live, NEW.job_role_family, NEW.job_first_seen_at
          FROM public.jobs jp
         WHERE jp.id = NEW.job_posting_id;
    END IF;
    RETURN NEW;
END;
$function$;

-- One-shot re-feed of the existing denorm to the new semantics. Writes only
-- rows whose value actually changes (rows where the provider date differs
-- from discovery time — the late-discovered-board cohort).
UPDATE public.scores s
   SET job_first_seen_at = COALESCE(jp.source_posted_at, jp.cataloged_at)
  FROM public.jobs jp
 WHERE jp.id = s.job_posting_id
   AND s.job_first_seen_at IS DISTINCT FROM COALESCE(jp.source_posted_at, jp.cataloged_at);

-- ---- 3. List RPCs: ONE re-issue carrying the renames + PR-a's tag columns --
-- Return-column changes require DROP + CREATE (OR REPLACE can't alter OUT
-- columns). Wire compatibility: the p_sort token 'created_at' is kept and
-- maps to cataloged_at — sort keys are enum tokens, not column names.
DROP FUNCTION IF EXISTS public.get_target_jobs(uuid, integer, text, text, text, text, boolean, integer, text, uuid, uuid);
DROP FUNCTION IF EXISTS public.get_cross_target_jobs(uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean, jsonb);

CREATE FUNCTION public.get_target_jobs(p_target_id uuid, p_min_score integer DEFAULT 0, p_status text DEFAULT NULL::text, p_company text DEFAULT NULL::text, p_search text DEFAULT NULL::text, p_sort text DEFAULT 'score'::text, p_ascending boolean DEFAULT false, p_limit integer DEFAULT 20, p_after_value text DEFAULT NULL::text, p_after_id uuid DEFAULT NULL::uuid, p_user_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(id uuid, external_id text, source_id uuid, title text, company_name text, location text, city text, state text, country text, location_remote boolean, department text, employment_type text, seniority text, metro text, is_remote boolean, absolute_url text, score integer, score_breakdown jsonb, scoring_status text, logistics_filters jsonb, status text, salary_text text, salary_min numeric, salary_max numeric, salary_currency text, salary_period text, source_posted_at timestamp with time zone, cataloged_at timestamp with time zone)
 LANGUAGE plpgsql
 STABLE
 SET search_path TO ''
AS $function$
DECLARE
    v_col    text;
    v_cast   text;
    v_dir    text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_cmp    text := CASE WHEN p_ascending THEN '>'  ELSE '<'   END;
    v_sql    text;
    v_family text;
BEGIN
    -- The target's role family (NULL = unclassified → the gate below is a no-op).
    SELECT t.role_family INTO v_family FROM public.targets AS t WHERE t.id = p_target_id;

    -- Whitelist the sort column + its cast (never interpolate user input).
    -- 'created_at' is the stable WIRE token; it sorts the renamed column.
    CASE p_sort
        WHEN 'created_at'   THEN v_col := 'jp.cataloged_at';  v_cast := 'timestamptz';
        WHEN 'company_name' THEN v_col := 'jp.company_name';  v_cast := 'text';
        WHEN 'title'        THEN v_col := 'jp.title';         v_cast := 'text';
        ELSE                     v_col := 's.score';          v_cast := 'integer';
    END CASE;

    v_sql := format($q$
        SELECT
            jp.id, jp.external_id, jp.source_id, jp.title,
            jp.company_name, jp.location,
            jp.city, jp.state, jp.country, jp.location_remote,
            jp.department,
            jp.employment_type, jp.seniority, jp.metro, jp.is_remote,
            jp.absolute_url, s.score, s.score_breakdown,
            s.scoring_status, s.logistics_filters,
            COALESCE(uj.status, 'new')::text AS status,
            jp.salary_text, jp.salary_min, jp.salary_max,
            jp.salary_currency, jp.salary_period,
            jp.source_posted_at, jp.cataloged_at
        FROM public.scores s
        INNER JOIN public.jobs jp ON jp.id = s.job_posting_id
        LEFT JOIN public.user_jobs uj
            ON uj.job_posting_id = jp.id AND uj.user_id = $1
        WHERE s.target_id = $2
            AND s.excluded = FALSE
            AND s.score >= $3
            AND jp.archived_at IS NULL
            AND jp.is_us IS NOT FALSE
            AND ($10 IS NULL OR jp.role_family IS NULL OR jp.role_family = $10)
            AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
            AND ($5 IS NULL OR jp.company_name = $5)
            AND ($6 IS NULL OR jp.title ILIKE '%%' || $6 || '%%')
            AND ($7 IS NULL OR (%1$s, s.job_posting_id) %2$s ($7::%3$s, $8))
        ORDER BY %1$s %4$s, s.job_posting_id %4$s
        LIMIT $9
    $q$, v_col, v_cmp, v_cast, v_dir);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_id, p_min_score, p_status, p_company,
              p_search, p_after_value, p_after_id, p_limit, v_family;
END;
$function$;

CREATE FUNCTION public.get_cross_target_jobs(p_target_ids uuid[], p_min_score integer DEFAULT 0, p_status text DEFAULT NULL::text, p_company text DEFAULT NULL::text, p_search text DEFAULT NULL::text, p_sort text DEFAULT 'score'::text, p_ascending boolean DEFAULT false, p_limit integer DEFAULT 20, p_offset integer DEFAULT 0, p_user_id uuid DEFAULT NULL::uuid, p_recency_decay boolean DEFAULT false, p_weights jsonb DEFAULT '{}'::jsonb)
 RETURNS TABLE(id uuid, external_id text, source_id uuid, title text, company_name text, location text, city text, state text, country text, location_remote boolean, department text, employment_type text, seniority text, metro text, is_remote boolean, absolute_url text, score integer, raw_score integer, score_breakdown jsonb, scoring_status text, logistics_filters jsonb, status text, salary_text text, salary_min numeric, salary_max numeric, salary_currency text, salary_period text, source_posted_at timestamp with time zone, cataloged_at timestamp with time zone, pending boolean)
 LANGUAGE plpgsql
 STABLE
 SET search_path TO ''
AS $function$
DECLARE
    v_dir        text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_need_axis  boolean := (p_weights IS NOT NULL AND p_weights <> '{}'::jsonb);
    v_disp       text;
    v_axis_col   text;
    v_gk         text;
    v_order      text;
    v_page_jobs  boolean;
    v_page_join  text;
    v_page_filt  text;
    v_sql        text;
BEGIN
    IF v_need_axis THEN
        v_disp     := 'public.wyrdfold_display_score(b.axis_scores, b.score, $9 -> b.target_id::text)';
        v_axis_col := ', s.axis_scores';
    ELSE
        v_disp     := 'b.score';
        v_axis_col := '';
    END IF;

    -- Graded key: the display value, or age-decayed when decay is on (prod).
    -- b.job_first_seen_at is the scores denorm — since R2 it carries
    -- COALESCE(source_posted_at, cataloged_at), so decay runs on the
    -- provider's posted date where known. NULL ⇒ age 0.
    IF p_recency_decay THEN
        v_gk := v_disp || ' * GREATEST(0.3, 1.0 - GREATEST(0.0, COALESCE(EXTRACT(EPOCH FROM (now() - b.job_first_seen_at)) / 86400.0, 0.0) - 7.0) * 0.015)';
    ELSE
        v_gk := v_disp;
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
                s.job_posting_id, s.target_id, s.score, s.scoring_status,
                s.is_graded, s.job_role_family, s.job_first_seen_at%4$s
            FROM public.scores s
            WHERE s.target_id = ANY ($2)
                AND s.job_is_live
                AND s.excluded = FALSE
                AND ($3 IS NULL
                     OR s.scoring_status IS DISTINCT FROM 'complete'
                     OR s.score >= $3)
            ORDER BY s.job_posting_id, s.is_graded DESC, s.score DESC
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
            jp.department,
            jp.employment_type, jp.seniority, jp.metro, jp.is_remote,
            jp.absolute_url,
            %5$s AS score, b.score AS raw_score, s2.score_breakdown,
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
    $q$, v_order, v_page_join, v_page_filt, v_axis_col, v_disp);

    RETURN QUERY EXECUTE v_sql
        USING p_user_id, p_target_ids, p_min_score, p_status, p_company,
              p_search, p_limit, p_offset, p_weights;
END;
$function$;

-- Grants mirror the previous definitions (list reads for API roles).
GRANT EXECUTE ON FUNCTION public.get_target_jobs(uuid, integer, text, text, text, text, boolean, integer, text, uuid, uuid) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_cross_target_jobs(uuid[], integer, text, text, text, text, boolean, integer, integer, uuid, boolean, jsonb) TO anon, authenticated, service_role;


-- ---- 4. Insights RPCs referenced j.created_at — re-issued from the LIVE ----
-- definitions with only the jobs column renamed; OUTPUT aliases keep the old
-- names so the Python readers' row keys are unchanged.
CREATE OR REPLACE FUNCTION public.insights_pipeline_status_counts(p_target_ids uuid[], p_since timestamp with time zone, p_until timestamp with time zone, p_user_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(status text, count bigint)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public', 'pg_catalog'
AS $function$
  SELECT COALESCE(uj.status, 'new') AS status, COUNT(DISTINCT s.job_posting_id)
  FROM   public.scores s
  JOIN   public.jobs j ON j.id = s.job_posting_id
  LEFT JOIN public.user_jobs uj
    ON uj.job_posting_id = j.id AND uj.user_id = p_user_id
  WHERE  s.target_id = ANY (p_target_ids)
    AND  s.excluded = false
    AND  (p_since IS NULL OR j.cataloged_at >= p_since)
    AND  (p_until IS NULL OR j.cataloged_at <  p_until)
  GROUP BY COALESCE(uj.status, 'new');
$function$;

CREATE OR REPLACE FUNCTION public.insights_targets_groupby(p_target_ids uuid[], p_since timestamp with time zone, p_user_id uuid DEFAULT NULL::uuid)
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO 'public', 'pg_catalog'
AS $function$
  WITH member AS (
    -- Raw membership: one row per non-excluded (posting, target) score in the
    -- window, NO targets join (mirrors Python's `membership` / `score_lookup`,
    -- which are not filtered by target existence). Drives the trend's per-
    -- posting MAX exactly as the Python does.
    SELECT s.job_posting_id           AS posting_id,
           s.target_id                AS target_id,
           COALESCE(s.score, 0)::int  AS score,
           j.cataloged_at               AS created_at
    FROM   public.scores s
    JOIN   public.jobs   j ON j.id = s.job_posting_id
    WHERE  s.target_id = ANY (p_target_ids)
      AND  s.excluded = false
      AND  (p_since IS NULL OR j.cataloged_at >= p_since)
  ),
  base AS (
    -- target_labels-filtered membership (the `if tid not in target_labels:
    -- continue` guard) with the per-user status overlaid. Mirrors
    -- target_jobs[tid] entries 1:1; drives per-target metrics, distribution
    -- and the unscored count.
    SELECT m.posting_id,
           m.target_id,
           t.label                       AS label,
           m.score                       AS score,
           COALESCE(uj.status, 'new')    AS status
    FROM   member m
    JOIN   public.targets t ON t.id = m.target_id
    LEFT JOIN public.user_jobs uj
      ON uj.job_posting_id = m.posting_id AND uj.user_id = p_user_id
  ),
  per_target AS (
    SELECT target_id,
           label,
           COUNT(*)                                                AS job_count,
           SUM(score)::bigint                                      AS score_sum,
           COUNT(*)                                                AS score_n,
           COUNT(*) FILTER (
             WHERE status IN ('applied', 'interviewing', 'offer')
           )                                                       AS applied_count,
           COUNT(*) FILTER (
             WHERE status IN ('interviewing', 'offer')
           )                                                       AS interview_count
    FROM   base
    GROUP BY target_id, label
  ),
  distribution AS (
    -- Only nonzero scores contribute (the Python `if score:` gate). Integer
    -- bucketing is exact, so it is safe to compute server-side.
    SELECT LEAST(GREATEST(LEAST(score, 100), 0) / 10, 9) AS bucket_idx,
           COUNT(*)                                       AS count
    FROM   base
    WHERE  score <> 0
    GROUP BY LEAST(GREATEST(LEAST(score, 100), 0) / 10, 9)
  ),
  unscored_posting AS (
    -- One row per posting (target_labels-filtered) recording whether it saw
    -- any nonzero score. unscored = those that saw none (Python's
    -- `if not seen_any_score`).
    SELECT posting_id,
           bool_or(score <> 0) AS seen_any_score
    FROM   base
    GROUP BY posting_id
  ),
  trend_posting AS (
    -- One row per posting with its best RAW-membership score and the job's
    -- week (Python's per-posting `best = max(per_target, default=0)`).
    SELECT posting_id,
           MAX(score)     AS best_score,
           MAX(created_at) AS created_at
    FROM   member
    GROUP BY posting_id
  ),
  trend AS (
    SELECT date_trunc('week', created_at)::date AS week_start,
           SUM(best_score)::bigint              AS score_sum,
           COUNT(*)                              AS score_n
    FROM   trend_posting
    WHERE  best_score > 0
    GROUP BY date_trunc('week', created_at)::date
  )
  SELECT jsonb_build_object(
    'targets', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'target_id',       target_id,
               'label',           label,
               'job_count',       job_count,
               'score_sum',       score_sum,
               'score_n',         score_n,
               'applied_count',   applied_count,
               'interview_count', interview_count
             ))
      FROM per_target
    ), '[]'::jsonb),
    'distribution', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'bucket_idx', bucket_idx,
               'count',      count
             ))
      FROM distribution
    ), '[]'::jsonb),
    'trend', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'week_start', week_start,
               'score_sum',  score_sum,
               'score_n',    score_n
             ))
      FROM trend
    ), '[]'::jsonb),
    'unscored', COALESCE((
      SELECT COUNT(*) FROM unscored_posting WHERE seen_any_score = false
    ), 0)
  );
$function$;


