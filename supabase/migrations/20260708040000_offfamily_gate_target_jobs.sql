-- #60 off-family relevance gate: drop off-role-family jobs from a target's list.
--
-- Confirmed leak: the "Senior Frontend Engineer" target showed ~34% OFF-family
-- matches — finance, legal, sales, HR, operations, marketing jobs scored 45-50 by
-- the LLM as "partial matches". role_family is tagged on jobs (the #60 tagger) but
-- was never enforced against the target the job is shown for. This mirrors the
-- #257 is_us gate, but per-target: a target now carries its own role_family
-- (derived from its label), and get_target_jobs keeps a job only when:
--   * the job's family MATCHES the target's, OR
--   * the job's family is NULL (untagged — benefit of the doubt, exactly like
--     `is_us IS NOT FALSE`), OR
--   * the target itself is unclassified (role_family NULL → ungated; the safe
--     default so a target without a family never hides everything).
-- STRICT match (design decision): a frontend search shouldn't surface
-- data_ml/product/design; adjacency adds fuzziness for little gain.
--
-- Scope: this gates the per-target list (the primary /jobs surface). The
-- untargeted dashboard (Python two-query path) + auto-deriving a NEW target's
-- family at creation are tracked follow-ons.

ALTER TABLE public.targets ADD COLUMN IF NOT EXISTS role_family text;

-- Backfill existing targets from their labels (idempotent; also applied to prod
-- directly. No-op on a fresh DB lacking these labels — new targets get their
-- family wired at creation in the follow-on).
UPDATE public.targets SET role_family = 'engineering'
  WHERE role_family IS NULL AND label IN (
    'Senior Frontend Engineer – Performance & Architecture',
    'Senior Full-Stack Engineer', 'Staff Frontend Engineer');
UPDATE public.targets SET role_family = 'customer_experience'
  WHERE role_family IS NULL AND label = 'Director of CX Operations & Transformation';
UPDATE public.targets SET role_family = 'product'
  WHERE role_family IS NULL AND label IN ('Chief Product Officer', 'VP of Product – AI & Data Products');
UPDATE public.targets SET role_family = 'other'
  WHERE role_family IS NULL AND label = 'Creative Writing Instructor';

-- Recreate get_target_jobs verbatim (from 20260701000000 / 20260707120000) with
-- the family gate added next to the is_us gate. Resolve the target's family into
-- v_family once, pass it as $10; the gate is a no-op when v_family IS NULL.
CREATE OR REPLACE FUNCTION public.get_target_jobs(
    p_target_id uuid,
    p_min_score integer DEFAULT 0,
    p_status text DEFAULT NULL,
    p_company text DEFAULT NULL,
    p_search text DEFAULT NULL,
    p_sort text DEFAULT 'score',
    p_ascending boolean DEFAULT false,
    p_limit integer DEFAULT 20,
    p_after_value text DEFAULT NULL,
    p_after_id uuid DEFAULT NULL,
    p_user_id uuid DEFAULT NULL
) RETURNS TABLE(
    id uuid, external_id text, source_id uuid, title text,
    company_name text, location text, department text,
    absolute_url text, score integer, score_breakdown jsonb,
    scoring_status text, logistics_filters jsonb, status text, salary_text text,
    greenhouse_updated_at timestamp with time zone,
    first_seen_at timestamp with time zone,
    created_at timestamp with time zone
)
    LANGUAGE plpgsql STABLE
    SET search_path TO ''
    AS $func$
DECLARE
    v_col    text;
    v_cast   text;
    v_dir    text := CASE WHEN p_ascending THEN 'ASC' ELSE 'DESC' END;
    v_cmp    text := CASE WHEN p_ascending THEN '>'  ELSE '<'   END;
    v_sql    text;
    v_family text;
BEGIN
    -- The target's role family (NULL = unclassified → the gate below is a no-op).
    -- Alias the table: ``id`` is also an OUT-param name, so an unqualified
    -- ``WHERE id = …`` is ambiguous (PL/pgSQL variable vs column).
    SELECT t.role_family INTO v_family FROM public.targets AS t WHERE t.id = p_target_id;

    -- Whitelist the sort column + its cast (never interpolate user input).
    CASE p_sort
        WHEN 'created_at'   THEN v_col := 'jp.created_at';   v_cast := 'timestamptz';
        WHEN 'company_name' THEN v_col := 'jp.company_name'; v_cast := 'text';
        WHEN 'title'        THEN v_col := 'jp.title';        v_cast := 'text';
        ELSE                     v_col := 's.score';         v_cast := 'integer';
    END CASE;

    v_sql := format($q$
        SELECT
            jp.id, jp.external_id, jp.source_id, jp.title,
            jp.company_name, jp.location, jp.department,
            jp.absolute_url, s.score, s.score_breakdown,
            s.scoring_status, s.logistics_filters,
            COALESCE(uj.status, 'new')::text AS status,
            jp.salary_text, jp.greenhouse_updated_at, jp.first_seen_at,
            jp.created_at
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
$func$;
