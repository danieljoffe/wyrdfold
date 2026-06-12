


SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "pg_trgm" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "supabase_vault" WITH SCHEMA "vault";






CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "vector" WITH SCHEMA "extensions";






CREATE OR REPLACE FUNCTION "public"."bulk_update_recency_scores"("p_updates" "jsonb") RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
declare
  cnt integer;
begin
  if p_updates is null or jsonb_array_length(p_updates) = 0 then
    return 0;
  end if;

  with u as (
    select (elem->>'id')::uuid            as id,
           (elem->>'recency_score')::int  as recency_score
    from   jsonb_array_elements(p_updates) elem
  )
  update public.scores s
  set    recency_score = u.recency_score
  from   u
  where  s.id = u.id;

  get diagnostics cnt = row_count;
  return cnt;
end;
$$;


ALTER FUNCTION "public"."bulk_update_recency_scores"("p_updates" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."bulk_update_salaries"("p_updates" "jsonb") RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
DECLARE
  cnt integer;
BEGIN
  IF p_updates IS NULL OR jsonb_array_length(p_updates) = 0 THEN
    RETURN 0;
  END IF;

  WITH u AS (
    SELECT (elem->>'id')::uuid       AS id,
           (elem->>'salary_text')    AS salary_text
    FROM   jsonb_array_elements(p_updates) elem
  )
  UPDATE public.jobs jp
  SET    salary_text = u.salary_text
  FROM   u
  WHERE  jp.id = u.id;

  GET DIAGNOSTICS cnt = ROW_COUNT;
  RETURN cnt;
END;
$$;


ALTER FUNCTION "public"."bulk_update_salaries"("p_updates" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."bulk_update_scores"("p_updates" "jsonb") RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
DECLARE
  cnt integer;
BEGIN
  IF p_updates IS NULL OR jsonb_array_length(p_updates) = 0 THEN
    RETURN 0;
  END IF;

  WITH u AS (
    SELECT (elem->>'id')::uuid    AS id,
           (elem->>'score')::int  AS score
    FROM   jsonb_array_elements(p_updates) elem
  )
  UPDATE public.jobs jp
  SET    score = u.score
  FROM   u
  WHERE  jp.id = u.id;

  GET DIAGNOSTICS cnt = ROW_COUNT;
  RETURN cnt;
END;
$$;


ALTER FUNCTION "public"."bulk_update_scores"("p_updates" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."get_target_jobs"("p_target_id" "uuid", "p_min_score" integer DEFAULT 0, "p_status" "text" DEFAULT NULL::"text", "p_company" "text" DEFAULT NULL::"text", "p_search" "text" DEFAULT NULL::"text", "p_sort" "text" DEFAULT 'score'::"text", "p_ascending" boolean DEFAULT false, "p_limit" integer DEFAULT 20, "p_offset" integer DEFAULT 0) RETURNS TABLE("id" "uuid", "external_id" "text", "source_id" "uuid", "title" "text", "company_name" "text", "location" "text", "department" "text", "absolute_url" "text", "score" integer, "score_breakdown" "jsonb", "scoring_status" "text", "status" "text", "salary_text" "text", "greenhouse_updated_at" timestamp with time zone, "first_seen_at" timestamp with time zone, "created_at" timestamp with time zone, "total_count" bigint)
    LANGUAGE "plpgsql" STABLE
    SET "search_path" TO ''
    AS $$
BEGIN
  RETURN QUERY
  SELECT
    jp.id,
    jp.external_id,
    jp.source_id,
    jp.title,
    jp.company_name,
    jp.location,
    jp.department,
    jp.absolute_url,
    s.score,
    s.score_breakdown,
    s.scoring_status,
    jp.status,
    jp.salary_text,
    jp.greenhouse_updated_at,
    jp.first_seen_at,
    jp.created_at,
    COUNT(*) OVER () AS total_count
  FROM public.scores s
  INNER JOIN public.jobs jp ON jp.id = s.job_posting_id
  WHERE s.target_id = p_target_id
    AND s.excluded = FALSE
    AND s.score >= p_min_score
    AND (p_status IS NULL OR jp.status = p_status)
    AND (p_company IS NULL OR jp.company_name = p_company)
    AND (p_search IS NULL OR jp.title ILIKE '%' || p_search || '%')
  ORDER BY
    CASE WHEN p_sort = 'score' AND NOT p_ascending THEN s.score END DESC NULLS LAST,
    CASE WHEN p_sort = 'score' AND p_ascending THEN s.score END ASC NULLS LAST,
    CASE WHEN p_sort = 'created_at' AND NOT p_ascending THEN jp.created_at END DESC NULLS LAST,
    CASE WHEN p_sort = 'created_at' AND p_ascending THEN jp.created_at END ASC NULLS LAST,
    CASE WHEN p_sort = 'company_name' AND NOT p_ascending THEN jp.company_name END DESC NULLS LAST,
    CASE WHEN p_sort = 'company_name' AND p_ascending THEN jp.company_name END ASC NULLS LAST,
    CASE WHEN p_sort = 'title' AND NOT p_ascending THEN jp.title END DESC NULLS LAST,
    CASE WHEN p_sort = 'title' AND p_ascending THEN jp.title END ASC NULLS LAST,
    -- Deterministic tiebreaker (PR #733) — every row has exactly one
    -- position in the result set so LIMIT/OFFSET paging is disjoint.
    jp.id ASC
  LIMIT p_limit
  OFFSET p_offset;
END;
$$;


ALTER FUNCTION "public"."get_target_jobs"("p_target_id" "uuid", "p_min_score" integer, "p_status" "text", "p_company" "text", "p_search" "text", "p_sort" "text", "p_ascending" boolean, "p_limit" integer, "p_offset" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
DECLARE
  user_email text;
BEGIN
  user_email := lower(event->'user'->>'email');

  IF user_email IS NULL THEN
    RETURN jsonb_build_object(
      'error', jsonb_build_object(
        'message', 'Email is required to sign up.',
        'http_code', 400
      )
    );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM public.wyrdfold_beta_invites
    WHERE lower(email) = user_email
  ) THEN
    -- Match GoTrue's standard "user not found" error verbatim.
    RETURN jsonb_build_object(
      'error', jsonb_build_object(
        'message', 'User not found',
        'http_code', 400
      )
    );
  END IF;

  RETURN '{}'::jsonb;
END;
$$;


ALTER FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."insert_source_if_not_exists"("p_provider" "text", "p_board_token" "text", "p_company_name" "text") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
DECLARE
  v_inserted boolean;
BEGIN
  INSERT INTO public.sources (provider, board_token, company_name, enabled)
  VALUES (p_provider, p_board_token, p_company_name, TRUE)
  ON CONFLICT (board_token) DO NOTHING;
  -- ``FOUND`` reflects whether the most recent INSERT/UPDATE/DELETE
  -- affected any rows. ON CONFLICT DO NOTHING sets it to FALSE when the
  -- conflict triggered the no-op branch.
  v_inserted := FOUND;
  RETURN v_inserted;
END;
$$;


ALTER FUNCTION "public"."insert_source_if_not_exists"("p_provider" "text", "p_board_token" "text", "p_company_name" "text") OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "public"."targets" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "label" "text" NOT NULL,
    "scoring_profile" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "is_active" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "updated_at" timestamp with time zone DEFAULT "now"(),
    "search_keywords" "jsonb" DEFAULT '[]'::"jsonb",
    "activation_status" "text" DEFAULT 'idle'::"text" NOT NULL,
    "description" "text",
    "normalized_label" "text",
    "profile_version" integer DEFAULT 1 NOT NULL,
    "example_promising_titles" "text"[] DEFAULT '{}'::"text"[] NOT NULL,
    "example_unpromising_titles" "text"[] DEFAULT '{}'::"text"[] NOT NULL,
    "seniority_hint" "text",
    "domain_hints" "text"[] DEFAULT '{}'::"text"[],
    CONSTRAINT "job_targets_activation_status_check" CHECK (("activation_status" = ANY (ARRAY['idle'::"text", 'deriving'::"text", 'polling'::"text", 'ready'::"text", 'error'::"text"])))
);


ALTER TABLE "public"."targets" OWNER TO "postgres";


COMMENT ON COLUMN "public"."targets"."seniority_hint" IS 'Single canonical seniority level for the slim target shape: one of ic, senior, staff, manager, director, vp, c_level. NULL on legacy targets (will be backfilled in PR B). Replaces the freeform scoring_profile.seniority.signals list as the source of truth for Phase 2 seniority_fit calibration.';



COMMENT ON COLUMN "public"."targets"."domain_hints" IS 'Industries / verticals / product types relevant to this target (e.g. [''SaaS'', ''DTC'', ''fintech'']). Replaces scoring_profile.domain.signals; empty array means domain-agnostic. Used by Phase 2''s domain_fit axis.';



CREATE OR REPLACE FUNCTION "public"."match_target_by_label"("query_label" "text", "threshold" double precision DEFAULT 0.7) RETURNS SETOF "public"."targets"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'extensions'
    AS $$
  SELECT *
  FROM public.targets
  WHERE similarity(normalized_label, query_label) >= threshold
  ORDER BY similarity(normalized_label, query_label) DESC
  LIMIT 1;
$$;


ALTER FUNCTION "public"."match_target_by_label"("query_label" "text", "threshold" double precision) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."rls_auto_enable"() RETURNS "event_trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog'
    AS $$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$$;


ALTER FUNCTION "public"."rls_auto_enable"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."set_job_feedback_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
begin
  new.updated_at := now();
  return new;
end;
$$;


ALTER FUNCTION "public"."set_job_feedback_updated_at"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."set_target_learning_log_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
begin
  new.updated_at := now();
  return new;
end;
$$;


ALTER FUNCTION "public"."set_target_learning_log_updated_at"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."set_user_profiles_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."set_user_profiles_updated_at"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(
           jsonb_object_agg(purpose, total),
           '{}'::jsonb
         )
  FROM (
    SELECT purpose,
           SUM(cost_usd)::numeric AS total
    FROM   public.llm_costs
    WHERE  (
             (p_user_id IS NULL AND user_id IS NULL)
             OR user_id = p_user_id
           )
      AND  (p_since IS NULL OR created_at >= p_since)
    GROUP BY purpose
  ) g;
$$;


ALTER FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."sync_target_active"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'public', 'extensions'
    AS $$
DECLARE
  _target_id UUID;
BEGIN
  IF TG_OP = 'DELETE' THEN
    _target_id := OLD.target_id;
  ELSE
    _target_id := NEW.target_id;
  END IF;

  UPDATE public.targets SET is_active = EXISTS(
    SELECT 1 FROM public.user_targets
    WHERE target_id = _target_id AND is_active = TRUE
  ) WHERE id = _target_id;

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."sync_target_active"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) RETURNS numeric
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_catalog'
    AS $$
  SELECT COALESCE(SUM(cost_usd), 0)::numeric
  FROM   public.llm_costs
  WHERE  (
           (p_user_id IS NULL AND user_id IS NULL)
           OR user_id = p_user_id
         )
    AND  (p_since IS NULL OR created_at >= p_since);
$$;


ALTER FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."analyses" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "job_posting_id" "uuid" NOT NULL,
    "user_id" "uuid",
    "optimized_doc_id" "uuid",
    "scorecard" "jsonb" NOT NULL,
    "recommendation" "text" NOT NULL,
    "model" "text" NOT NULL,
    "cost_usd" numeric DEFAULT 0 NOT NULL,
    "latency_ms" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "target_id" "uuid" NOT NULL
);


ALTER TABLE "public"."analyses" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."batch_runs" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "total" integer NOT NULL,
    "completed" integer DEFAULT 0 NOT NULL,
    "failed" integer DEFAULT 0 NOT NULL,
    "items" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "batch_jobs_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'processing'::"text", 'completed'::"text", 'failed'::"text"])))
);


ALTER TABLE "public"."batch_runs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."document_versions" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "resume_id" "uuid" NOT NULL,
    "payload" "jsonb" NOT NULL,
    "source" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "payload_md" "text",
    CONSTRAINT "tailored_resume_versions_source_check" CHECK (("source" = ANY (ARRAY['initial'::"text", 'user_edit'::"text", 'llm_adapt'::"text"])))
);


ALTER TABLE "public"."document_versions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."documents" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "job_posting_id" "uuid",
    "resume_type" "text" NOT NULL,
    "jd_snapshot" "text" NOT NULL,
    "jd_snapshot_hash" "text" NOT NULL,
    "payload" "jsonb" NOT NULL,
    "storage_path" "text",
    "warnings" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "model" "text",
    "input_tokens" integer DEFAULT 0,
    "output_tokens" integer DEFAULT 0,
    "cost_usd" numeric(10,6) DEFAULT 0,
    "latency_ms" integer DEFAULT 0,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "document_type" "text" DEFAULT 'resume'::"text" NOT NULL,
    "source_resume_id" "uuid",
    "updated_at" timestamp with time zone,
    "approved_at" timestamp with time zone,
    "payload_md" "text",
    "docx_payload_md_hash" "text",
    "style_settings" "jsonb",
    CONSTRAINT "tailored_resumes_document_type_check" CHECK (("document_type" = ANY (ARRAY['resume'::"text", 'cover_letter'::"text"])))
);


ALTER TABLE "public"."documents" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."experience_chunks" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "optimized_doc_id" "uuid" NOT NULL,
    "chunk_type" "text" NOT NULL,
    "chunk_ref" "text" NOT NULL,
    "content" "text" NOT NULL,
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "embedding" "extensions"."vector"(1024),
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "experience_chunks_chunk_type_check" CHECK (("chunk_type" = ANY (ARRAY['role'::"text", 'skill'::"text", 'outcome'::"text", 'summary'::"text"])))
);


ALTER TABLE "public"."experience_chunks" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."experience_conversation_turns" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "conversation_type" "text" NOT NULL,
    "turn_index" integer NOT NULL,
    "role" "text" NOT NULL,
    "content" "text" NOT NULL,
    "skipped" boolean DEFAULT false NOT NULL,
    "prose_doc_id" "uuid",
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "experience_conversation_turns_conversation_type_check" CHECK (("conversation_type" = ANY (ARRAY['onboarding'::"text", 'update'::"text"]))),
    CONSTRAINT "experience_conversation_turns_role_check" CHECK (("role" = ANY (ARRAY['user'::"text", 'assistant'::"text", 'system'::"text"])))
);


ALTER TABLE "public"."experience_conversation_turns" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."experience_optimized_docs" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "prose_doc_id" "uuid",
    "version" integer NOT NULL,
    "payload" "jsonb" NOT NULL,
    "markdown_view" "text",
    "source" "text" DEFAULT 'llm'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "experience_optimized_docs_source_check" CHECK (("source" = ANY (ARRAY['llm'::"text", 'user_edit'::"text"])))
);


ALTER TABLE "public"."experience_optimized_docs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."experience_preferences" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "payload" "jsonb" DEFAULT '{"avoid": [], "rules": [], "tone_notes": []}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."experience_preferences" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."experience_prose_docs" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "version" integer NOT NULL,
    "content" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."experience_prose_docs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."job_feedback" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "text" NOT NULL,
    "job_posting_id" "uuid" NOT NULL,
    "target_id" "uuid" NOT NULL,
    "signal" "text" NOT NULL,
    "reason" "text",
    "applied_at" timestamp with time zone,
    "applied_run_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "job_feedback_signal_check" CHECK (("signal" = ANY (ARRAY['irrelevant'::"text", 'relevant'::"text"])))
);


ALTER TABLE "public"."job_feedback" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."jobs" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "external_id" "text" NOT NULL,
    "source_id" "uuid" NOT NULL,
    "title" "text" NOT NULL,
    "company_name" "text" NOT NULL,
    "location" "text",
    "department" "text",
    "description_html" "text",
    "absolute_url" "text",
    "score" integer DEFAULT 0 NOT NULL,
    "score_breakdown" "jsonb",
    "status" "text" DEFAULT 'new'::"text" NOT NULL,
    "greenhouse_updated_at" timestamp with time zone,
    "first_seen_at" timestamp with time zone DEFAULT "now"(),
    "created_at" timestamp with time zone DEFAULT "now"(),
    "updated_at" timestamp with time zone DEFAULT "now"(),
    "url_validation_status" "text",
    "url_validation_warnings" "jsonb" DEFAULT '[]'::"jsonb",
    "target_id" "uuid",
    "salary_text" "text",
    "llm_score" double precision,
    "llm_analysis_id" "uuid",
    "last_url_check_at" timestamp with time zone,
    "url_check_status" integer,
    "url_check_failure_count" integer DEFAULT 0 NOT NULL,
    CONSTRAINT "job_postings_url_validation_status_check" CHECK (("url_validation_status" = ANY (ARRAY['valid'::"text", 'rejected'::"text"]))),
    CONSTRAINT "jobs_score_check" CHECK ((("score" >= 0) AND ("score" <= 100))),
    CONSTRAINT "jobs_status_check" CHECK (("status" = ANY (ARRAY['new'::"text", 'saved'::"text", 'resume_draft'::"text", 'resume_ready'::"text", 'applied'::"text", 'interviewing'::"text", 'offer'::"text", 'rejected'::"text", 'archived'::"text"])))
);


ALTER TABLE "public"."jobs" OWNER TO "postgres";


COMMENT ON COLUMN "public"."jobs"."last_url_check_at" IS 'Timestamp of the most recent URL health check. NULL for jobs ingested before url_health shipped, or never-checked. The scheduler picks the oldest first.';



COMMENT ON COLUMN "public"."jobs"."url_check_status" IS 'Most recent HTTP status code observed by the URL health check. NULL = never checked. 0 = network error / unreachable. 2xx = healthy. 4xx = dead-link signal (archived after url_check_failure_count >= threshold).';



COMMENT ON COLUMN "public"."jobs"."url_check_failure_count" IS 'Consecutive 4xx / network-error count. Reset to 0 on the next 2xx. Archives the job when it reaches URL_HEALTH_FAILURE_THRESHOLD (default 3) to avoid a single transient failure killing a live posting.';



CREATE TABLE IF NOT EXISTS "public"."llm_costs" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "model" "text" NOT NULL,
    "purpose" "text" NOT NULL,
    "input_tokens" integer DEFAULT 0 NOT NULL,
    "output_tokens" integer DEFAULT 0 NOT NULL,
    "cache_read_input_tokens" integer DEFAULT 0 NOT NULL,
    "cache_creation_input_tokens" integer DEFAULT 0 NOT NULL,
    "cost_usd" numeric(10,6) DEFAULT 0 NOT NULL,
    "latency_ms" integer DEFAULT 0 NOT NULL,
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."llm_costs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."notifications_sent" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_profile_id" "uuid" NOT NULL,
    "job_posting_id" "uuid" NOT NULL,
    "score_at_send" integer NOT NULL,
    "external_id" "text",
    "sent_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "channel" "text" DEFAULT 'email'::"text" NOT NULL,
    CONSTRAINT "job_notification_sent_score_at_send_check" CHECK ((("score_at_send" >= 0) AND ("score_at_send" <= 100))),
    CONSTRAINT "notifications_sent_channel_check" CHECK (("channel" = ANY (ARRAY['email'::"text", 'sms'::"text"])))
);


ALTER TABLE "public"."notifications_sent" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."reference_jds" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "target_id" "uuid" NOT NULL,
    "jd_url" "text",
    "jd_text" "text" NOT NULL,
    "extracted_profile" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."reference_jds" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."scores" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "job_posting_id" "uuid" NOT NULL,
    "target_id" "uuid" NOT NULL,
    "score" integer DEFAULT 0 NOT NULL,
    "score_breakdown" "jsonb",
    "matched_keywords" "text"[] DEFAULT '{}'::"text"[],
    "excluded" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "scoring_status" "text" DEFAULT 'stage1'::"text" NOT NULL,
    "scored_profile_version" integer DEFAULT 1 NOT NULL,
    "promising" boolean,
    "axis_scores" "jsonb",
    "fit_reasoning" "text",
    "recency_score" integer,
    "phase1_confidence" integer,
    "logistics_filters" "jsonb",
    CONSTRAINT "job_target_scores_scoring_status_check" CHECK (("scoring_status" = ANY (ARRAY['stage1'::"text", 'stage2'::"text", 'complete'::"text"]))),
    CONSTRAINT "scores_phase1_confidence_check" CHECK ((("phase1_confidence" IS NULL) OR (("phase1_confidence" >= 0) AND ("phase1_confidence" <= 100))))
);


ALTER TABLE "public"."scores" OWNER TO "postgres";


COMMENT ON COLUMN "public"."scores"."promising" IS 'Phase 1 LLM title triage verdict. NULL = legacy/pre-Phase-1/fail-open, TRUE = admitted by Phase 1, FALSE = unused (rejected jobs are not ingested so no row exists).';



COMMENT ON COLUMN "public"."scores"."axis_scores" IS 'Phase 2 LLM grader output: {title_fit, skills_fit, seniority_fit, domain_fit} each 0-100. NULL when Phase 2 has not run for this row (legacy / pending / Phase 2 failed).';



COMMENT ON COLUMN "public"."scores"."fit_reasoning" IS 'Phase 2 LLM grader output: 1-2 sentence reasoning. Surfaced on the job detail panel. NULL when Phase 2 has not run.';



COMMENT ON COLUMN "public"."scores"."recency_score" IS 'Fit score (scores.score) decayed by posting age — the value the /jobs list sorts/paginates by. recency_score == score when RECENCY_DECAY_ENABLED is off. Refreshed by the poller each cycle from jobs.first_seen_at. See app/services/recency.py.';



COMMENT ON COLUMN "public"."scores"."phase1_confidence" IS 'Phase 1 LLM confidence (0-100) in its promising/unpromising verdict for this (job, target) pair. NULL on legacy rows or when the Phase 1 response lacked a confidence value. Used by phase2_runner to order candidates (highest confidence first) and by relevance diagnostics to validate whether Phase 1 confidence predicts Phase 2 score.';



COMMENT ON COLUMN "public"."scores"."logistics_filters" IS 'Structured logistics fields extracted by the Phase 2 grader: {remote_status, salary_min, salary_max, salary_currency, salary_unit, location_city, location_country}. Powers /jobs logistics chips and filter pills. Filter-only — NOT used in scoring or sort order. See plan-wyrdfold-logistics-chips.md for the extraction contract.';



CREATE TABLE IF NOT EXISTS "public"."source_discoveries" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "target_id" "uuid",
    "search_keyword" "text" NOT NULL,
    "ats_site_filter" "text",
    "source_url" "text" NOT NULL,
    "detected_provider" "text",
    "detected_board_token" "text",
    "detected_company_name" "text",
    "detected_job_count" integer,
    "outcome" "text" NOT NULL,
    "discovered_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."source_discoveries" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."sources" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "board_token" "text" NOT NULL,
    "company_name" "text" NOT NULL,
    "enabled" boolean DEFAULT true,
    "last_polled_at" timestamp with time zone,
    "job_count" integer DEFAULT 0,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "provider" "text" DEFAULT 'greenhouse'::"text" NOT NULL,
    "poll_interval_minutes" integer DEFAULT 240 NOT NULL,
    "consecutive_failures" integer DEFAULT 0 NOT NULL,
    "last_candidate_at" timestamp with time zone,
    CONSTRAINT "sources_poll_interval_minutes_check" CHECK ((("poll_interval_minutes" >= 5) AND ("poll_interval_minutes" <= 10080)))
);


ALTER TABLE "public"."sources" OWNER TO "postgres";


COMMENT ON COLUMN "public"."sources"."poll_interval_minutes" IS 'Minimum minutes between polls of this source. Used by poll_due_sources.';



COMMENT ON COLUMN "public"."sources"."consecutive_failures" IS 'Fetch failures since last success; the poller auto-disables at threshold.';



COMMENT ON COLUMN "public"."sources"."last_candidate_at" IS 'Last poll that upserted at least one candidate job. Drives the adaptive cadence sweep (cold sources stretch to daily polling).';



CREATE TABLE IF NOT EXISTS "public"."status_log" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "posting_id" "uuid" NOT NULL,
    "old_status" "text",
    "new_status" "text" NOT NULL,
    "note" "text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."status_log" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."target_derive_jd_cache" (
    "jd_hash" "text" NOT NULL,
    "prompt_version" "text" NOT NULL,
    "model" "text" NOT NULL,
    "derived_payload" "jsonb" NOT NULL,
    "hit_count" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "last_hit_at" timestamp with time zone
);


ALTER TABLE "public"."target_derive_jd_cache" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."target_learning_log" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "text" NOT NULL,
    "target_id" "uuid" NOT NULL,
    "status" "text" NOT NULL,
    "prev_profile" "jsonb" NOT NULL,
    "next_profile" "jsonb" NOT NULL,
    "diff" "jsonb" NOT NULL,
    "confidence" numeric(3,2) NOT NULL,
    "rationale" "text",
    "signals_consumed" integer DEFAULT 0 NOT NULL,
    "applied_run_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "target_learning_log_confidence_check" CHECK ((("confidence" >= (0)::numeric) AND ("confidence" <= (1)::numeric))),
    CONSTRAINT "target_learning_log_status_check" CHECK (("status" = ANY (ARRAY['applied'::"text", 'staged'::"text", 'rejected'::"text"])))
);


ALTER TABLE "public"."target_learning_log" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."uploaded_resumes" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "filename" "text" NOT NULL,
    "file_type" "text" NOT NULL,
    "storage_path" "text" NOT NULL,
    "extracted_text" "text" NOT NULL,
    "prose_doc_id" "uuid",
    "page_count" integer,
    "file_size_bytes" integer NOT NULL,
    "warnings" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "resume_uploads_file_type_check" CHECK (("file_type" = ANY (ARRAY['pdf'::"text", 'docx'::"text"])))
);


ALTER TABLE "public"."uploaded_resumes" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."user_profiles" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid",
    "email" "text",
    "job_score_threshold" integer DEFAULT 70 NOT NULL,
    "job_notifications_enabled" boolean DEFAULT true NOT NULL,
    "unsubscribed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "name" "text",
    "location" "text",
    "linkedin_url" "text",
    "website_url" "text",
    "phone_number" "text",
    "sms_notifications_enabled" boolean DEFAULT false NOT NULL,
    "sms_score_threshold" integer DEFAULT 85 NOT NULL,
    "sms_daily_limit" integer DEFAULT 5 NOT NULL,
    "resume_style_settings" "jsonb",
    "list_min_score" integer,
    "onboarding_completed_at" timestamp with time zone,
    "onboarding_path" "text",
    "onboarding_current_step" "text",
    "llm_monthly_budget_usd" numeric,
    "max_active_targets" integer,
    "last_seen_at" timestamp with time zone,
    "llm_enabled" boolean DEFAULT true NOT NULL,
    CONSTRAINT "user_profiles_job_score_threshold_check" CHECK ((("job_score_threshold" >= 0) AND ("job_score_threshold" <= 100))),
    CONSTRAINT "user_profiles_list_min_score_check" CHECK ((("list_min_score" IS NULL) OR (("list_min_score" >= 0) AND ("list_min_score" <= 100)))),
    CONSTRAINT "user_profiles_onboarding_path_check" CHECK ((("onboarding_path" IS NULL) OR ("onboarding_path" = ANY (ARRAY['A'::"text", 'B'::"text", 'C'::"text"])))),
    CONSTRAINT "user_profiles_sms_daily_limit_check" CHECK ((("sms_daily_limit" >= 1) AND ("sms_daily_limit" <= 50))),
    CONSTRAINT "user_profiles_sms_score_threshold_check" CHECK ((("sms_score_threshold" >= 0) AND ("sms_score_threshold" <= 100)))
);


ALTER TABLE "public"."user_profiles" OWNER TO "postgres";


COMMENT ON COLUMN "public"."user_profiles"."onboarding_completed_at" IS 'Set by the OnboardingWizard when the user finishes the wizard. NULL = user has not completed onboarding; the FE dashboard redirects them to /onboarding. Belt-and-suspenders: the dashboard also keeps a hasProse fallback check so a user with this set but no profile data still gets redirected (with a Sentry warning).';



COMMENT ON COLUMN "public"."user_profiles"."onboarding_path" IS 'Which of the three onboarding paths the user picked: A (full setup), B (upload resume + pick targets), or C (conversation + pick targets). NULL until the user makes a choice on the PathChooser step.';



COMMENT ON COLUMN "public"."user_profiles"."onboarding_current_step" IS 'Wizard step the user was last on (e.g. ''identity'', ''upload-resume'', ''pick-targets''). Updated on every wizard transition; the wizard reads it on mount to resume mid-flow. NULL = user has not started onboarding.';



COMMENT ON COLUMN "public"."user_profiles"."llm_monthly_budget_usd" IS 'Per-user monthly LLM allowance override (USD, rolling 30d). NULL = settings default.';



COMMENT ON COLUMN "public"."user_profiles"."max_active_targets" IS 'Per-user active-target cap override. NULL = settings default.';



COMMENT ON COLUMN "public"."user_profiles"."last_seen_at" IS 'Last authenticated request (throttled ~1/hour). Drives idle defer/deactivate.';



COMMENT ON COLUMN "public"."user_profiles"."llm_enabled" IS 'Operator kill-switch: FALSE blocks all LLM spend for this account (429 + poller defer).';



CREATE TABLE IF NOT EXISTS "public"."user_targets" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "text" NOT NULL,
    "target_id" "uuid" NOT NULL,
    "is_active" boolean DEFAULT false NOT NULL,
    "fit_score" integer,
    "fit_score_reasoning" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "axis_weights" "jsonb",
    "axis_weights_previous" "jsonb",
    "auto_deactivated_at" timestamp with time zone
);


ALTER TABLE "public"."user_targets" OWNER TO "postgres";


COMMENT ON COLUMN "public"."user_targets"."axis_weights" IS 'User-tunable weights applied to Phase 2 axis_scores at read time to produce display_score. JSONB shape: {"title_fit": 0.25, "skills_fit": 0.25, "seniority_fit": 0.25, "domain_fit": 0.25}. NULL means "use equal quartile" (Sonnet''s holistic score). Adjusting weights does NOT trigger re-grading; behavior is purely read-time math.';



COMMENT ON COLUMN "public"."user_targets"."axis_weights_previous" IS 'One-step-back snapshot for the undo button. Set automatically every time axis_weights changes (the prior value moves here). Only one previous state is retained — YAGNI on full history for v1.';



COMMENT ON COLUMN "public"."user_targets"."auto_deactivated_at" IS 'Set when the idle-lifecycle sweep deactivated this link (NULL = user action).';



CREATE TABLE IF NOT EXISTS "public"."wyrdfold_beta_invites" (
    "email" "text" NOT NULL,
    "invited_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "accepted_at" timestamp with time zone
);


ALTER TABLE "public"."wyrdfold_beta_invites" OWNER TO "postgres";


ALTER TABLE ONLY "public"."batch_runs"
    ADD CONSTRAINT "batch_jobs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."experience_chunks"
    ADD CONSTRAINT "experience_chunks_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."experience_conversation_turns"
    ADD CONSTRAINT "experience_conversation_turns_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."experience_optimized_docs"
    ADD CONSTRAINT "experience_optimized_docs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."experience_optimized_docs"
    ADD CONSTRAINT "experience_optimized_docs_user_id_version_key" UNIQUE ("user_id", "version");



ALTER TABLE ONLY "public"."experience_preferences"
    ADD CONSTRAINT "experience_preferences_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."experience_preferences"
    ADD CONSTRAINT "experience_preferences_user_id_key" UNIQUE ("user_id");



ALTER TABLE ONLY "public"."experience_prose_docs"
    ADD CONSTRAINT "experience_prose_docs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."experience_prose_docs"
    ADD CONSTRAINT "experience_prose_docs_user_id_version_key" UNIQUE ("user_id", "version");



ALTER TABLE ONLY "public"."analyses"
    ADD CONSTRAINT "job_analyses_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."job_feedback"
    ADD CONSTRAINT "job_feedback_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."job_feedback"
    ADD CONSTRAINT "job_feedback_user_id_job_posting_id_target_id_key" UNIQUE ("user_id", "job_posting_id", "target_id");



ALTER TABLE ONLY "public"."notifications_sent"
    ADD CONSTRAINT "job_notification_sent_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."jobs"
    ADD CONSTRAINT "job_postings_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."sources"
    ADD CONSTRAINT "job_sources_board_token_key" UNIQUE ("board_token");



ALTER TABLE ONLY "public"."sources"
    ADD CONSTRAINT "job_sources_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."status_log"
    ADD CONSTRAINT "job_status_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."scores"
    ADD CONSTRAINT "job_target_scores_job_posting_id_target_id_key" UNIQUE ("job_posting_id", "target_id");



ALTER TABLE ONLY "public"."scores"
    ADD CONSTRAINT "job_target_scores_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."targets"
    ADD CONSTRAINT "job_targets_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."jobs"
    ADD CONSTRAINT "jobs_source_external_unique" UNIQUE ("source_id", "external_id");



ALTER TABLE ONLY "public"."llm_costs"
    ADD CONSTRAINT "llm_cost_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."notifications_sent"
    ADD CONSTRAINT "notifications_sent_user_profile_job_channel_key" UNIQUE ("user_profile_id", "job_posting_id", "channel");



ALTER TABLE ONLY "public"."uploaded_resumes"
    ADD CONSTRAINT "resume_uploads_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."source_discoveries"
    ADD CONSTRAINT "source_discoveries_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."document_versions"
    ADD CONSTRAINT "tailored_resume_versions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."documents"
    ADD CONSTRAINT "tailored_resumes_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."target_derive_jd_cache"
    ADD CONSTRAINT "target_derive_jd_cache_pkey" PRIMARY KEY ("jd_hash");



ALTER TABLE ONLY "public"."target_learning_log"
    ADD CONSTRAINT "target_learning_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."reference_jds"
    ADD CONSTRAINT "target_reference_jds_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_email_key" UNIQUE ("email");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_profiles"
    ADD CONSTRAINT "user_profiles_user_id_key" UNIQUE ("user_id");



ALTER TABLE ONLY "public"."user_targets"
    ADD CONSTRAINT "user_targets_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_targets"
    ADD CONSTRAINT "user_targets_user_id_target_id_key" UNIQUE ("user_id", "target_id");



ALTER TABLE ONLY "public"."wyrdfold_beta_invites"
    ADD CONSTRAINT "wyrdfold_beta_invites_pkey" PRIMARY KEY ("email");



CREATE INDEX "idx_analyses_cache_lookup" ON "public"."analyses" USING "btree" ("job_posting_id", "user_id", "created_at" DESC);



CREATE INDEX "idx_batch_jobs_user" ON "public"."batch_runs" USING "btree" ("user_id");



CREATE INDEX "idx_experience_chunks_embedding" ON "public"."experience_chunks" USING "hnsw" ("embedding" "extensions"."vector_cosine_ops");



CREATE INDEX "idx_experience_chunks_optimized_doc" ON "public"."experience_chunks" USING "btree" ("optimized_doc_id");



CREATE INDEX "idx_experience_chunks_type" ON "public"."experience_chunks" USING "btree" ("chunk_type");



CREATE INDEX "idx_experience_optimized_user_version" ON "public"."experience_optimized_docs" USING "btree" ("user_id", "version" DESC);



CREATE INDEX "idx_experience_prose_user_version" ON "public"."experience_prose_docs" USING "btree" ("user_id", "version" DESC);



CREATE INDEX "idx_experience_turns_type" ON "public"."experience_conversation_turns" USING "btree" ("conversation_type");



CREATE INDEX "idx_experience_turns_user_created" ON "public"."experience_conversation_turns" USING "btree" ("user_id", "created_at");



CREATE INDEX "idx_job_analyses_cache_key" ON "public"."analyses" USING "btree" ("job_posting_id", "target_id", "optimized_doc_id");



CREATE INDEX "idx_job_analyses_job_id" ON "public"."analyses" USING "btree" ("job_posting_id");



CREATE INDEX "idx_job_feedback_target_unapplied" ON "public"."job_feedback" USING "btree" ("target_id") WHERE ("applied_at" IS NULL);



CREATE INDEX "idx_job_feedback_user_target_created" ON "public"."job_feedback" USING "btree" ("user_id", "target_id", "created_at" DESC);



CREATE INDEX "idx_job_notification_sent_job" ON "public"."notifications_sent" USING "btree" ("job_posting_id");



CREATE INDEX "idx_job_notification_sent_user" ON "public"."notifications_sent" USING "btree" ("user_profile_id");



CREATE INDEX "idx_job_postings_company_name" ON "public"."jobs" USING "btree" ("company_name");



CREATE INDEX "idx_job_postings_created_at" ON "public"."jobs" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_job_postings_external_id" ON "public"."jobs" USING "btree" ("external_id");



CREATE INDEX "idx_job_postings_llm_score" ON "public"."jobs" USING "btree" ("llm_score") WHERE ("llm_score" IS NOT NULL);



CREATE INDEX "idx_job_postings_score" ON "public"."jobs" USING "btree" ("score" DESC);



CREATE INDEX "idx_job_postings_source" ON "public"."jobs" USING "btree" ("source_id");



CREATE INDEX "idx_job_postings_status" ON "public"."jobs" USING "btree" ("status");



CREATE INDEX "idx_job_postings_target" ON "public"."jobs" USING "btree" ("target_id");



CREATE INDEX "idx_job_postings_title_trgm" ON "public"."jobs" USING "gin" ("title" "extensions"."gin_trgm_ops");



CREATE INDEX "idx_job_sources_enabled" ON "public"."sources" USING "btree" ("enabled");



CREATE INDEX "idx_job_status_log_created_at" ON "public"."status_log" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_job_status_log_posting" ON "public"."status_log" USING "btree" ("posting_id");



CREATE INDEX "idx_job_targets_active" ON "public"."targets" USING "btree" ("is_active", "created_at" DESC);



CREATE INDEX "idx_job_targets_normalized_label_trgm" ON "public"."targets" USING "gin" ("normalized_label" "extensions"."gin_trgm_ops");



CREATE INDEX "idx_jobs_status_score" ON "public"."jobs" USING "btree" ("status", "score" DESC);



CREATE INDEX "idx_jts_job" ON "public"."scores" USING "btree" ("job_posting_id");



CREATE INDEX "idx_jts_scoring_status" ON "public"."scores" USING "btree" ("scoring_status") WHERE ("scoring_status" <> 'complete'::"text");



CREATE INDEX "idx_jts_target" ON "public"."scores" USING "btree" ("target_id");



CREATE INDEX "idx_jts_target_score" ON "public"."scores" USING "btree" ("target_id", "excluded", "score" DESC);



CREATE INDEX "idx_llm_cost_log_created_at" ON "public"."llm_costs" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_llm_cost_log_user_purpose" ON "public"."llm_costs" USING "btree" ("user_id", "purpose", "created_at" DESC);



CREATE INDEX "idx_prose_user_version" ON "public"."experience_prose_docs" USING "btree" ("user_id", "version" DESC);



CREATE INDEX "idx_resume_uploads_user" ON "public"."uploaded_resumes" USING "btree" ("user_id");



CREATE INDEX "idx_sources_enabled_last_polled" ON "public"."sources" USING "btree" ("enabled", "last_polled_at");



CREATE INDEX "idx_tailored_resume_versions_resume_created" ON "public"."document_versions" USING "btree" ("resume_id", "created_at" DESC);



CREATE INDEX "idx_tailored_resumes_approved" ON "public"."documents" USING "btree" ("approved_at") WHERE ("approved_at" IS NOT NULL);



CREATE INDEX "idx_tailored_resumes_document_type" ON "public"."documents" USING "btree" ("document_type", "created_at" DESC);



CREATE INDEX "idx_tailored_resumes_jd_hash" ON "public"."documents" USING "btree" ("jd_snapshot_hash");



CREATE INDEX "idx_tailored_resumes_job" ON "public"."documents" USING "btree" ("job_posting_id");



CREATE INDEX "idx_tailored_resumes_source" ON "public"."documents" USING "btree" ("source_resume_id");



CREATE INDEX "idx_tailored_resumes_user_created" ON "public"."documents" USING "btree" ("user_id", "created_at" DESC);



CREATE INDEX "idx_target_derive_jd_cache_prompt_version" ON "public"."target_derive_jd_cache" USING "btree" ("prompt_version", "created_at" DESC);



CREATE INDEX "idx_target_learning_log_applied_run" ON "public"."target_learning_log" USING "btree" ("applied_run_id") WHERE ("applied_run_id" IS NOT NULL);



CREATE INDEX "idx_target_learning_log_user_target_status" ON "public"."target_learning_log" USING "btree" ("user_id", "target_id", "status", "created_at" DESC);



CREATE INDEX "idx_target_ref_jds_target" ON "public"."reference_jds" USING "btree" ("target_id");



CREATE INDEX "idx_targets_active_only" ON "public"."targets" USING "btree" ("is_active") WHERE ("is_active" = true);



CREATE INDEX "idx_user_profiles_email" ON "public"."user_profiles" USING "btree" ("email");



CREATE INDEX "idx_user_profiles_notifications" ON "public"."user_profiles" USING "btree" ("job_notifications_enabled") WHERE (("job_notifications_enabled" = true) AND ("unsubscribed_at" IS NULL));



CREATE INDEX "idx_user_targets_active" ON "public"."user_targets" USING "btree" ("user_id") WHERE ("is_active" = true);



CREATE INDEX "idx_user_targets_user" ON "public"."user_targets" USING "btree" ("user_id");



CREATE INDEX "jobs_url_health_due_idx" ON "public"."jobs" USING "btree" ("last_url_check_at" NULLS FIRST) WHERE ("status" <> ALL (ARRAY['archived'::"text", 'saved'::"text", 'applied'::"text"]));



CREATE INDEX "llm_costs_user_id_created_at_idx" ON "public"."llm_costs" USING "btree" ("user_id", "created_at" DESC);



CREATE INDEX "scores_target_recency_idx" ON "public"."scores" USING "btree" ("target_id", "recency_score" DESC);



CREATE INDEX "source_discoveries_discovered_at_idx" ON "public"."source_discoveries" USING "btree" ("discovered_at" DESC);



CREATE INDEX "source_discoveries_source_url_idx" ON "public"."source_discoveries" USING "btree" ("source_url");



CREATE INDEX "source_discoveries_target_id_idx" ON "public"."source_discoveries" USING "btree" ("target_id");



CREATE OR REPLACE TRIGGER "trg_job_feedback_updated_at" BEFORE UPDATE ON "public"."job_feedback" FOR EACH ROW EXECUTE FUNCTION "public"."set_job_feedback_updated_at"();



CREATE OR REPLACE TRIGGER "trg_sync_target_active" AFTER INSERT OR DELETE OR UPDATE OF "is_active" ON "public"."user_targets" FOR EACH ROW EXECUTE FUNCTION "public"."sync_target_active"();



CREATE OR REPLACE TRIGGER "trg_target_learning_log_updated_at" BEFORE UPDATE ON "public"."target_learning_log" FOR EACH ROW EXECUTE FUNCTION "public"."set_target_learning_log_updated_at"();



CREATE OR REPLACE TRIGGER "trg_user_profiles_updated_at" BEFORE UPDATE ON "public"."user_profiles" FOR EACH ROW EXECUTE FUNCTION "public"."set_user_profiles_updated_at"();



ALTER TABLE ONLY "public"."experience_chunks"
    ADD CONSTRAINT "experience_chunks_optimized_doc_id_fkey" FOREIGN KEY ("optimized_doc_id") REFERENCES "public"."experience_optimized_docs"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."experience_conversation_turns"
    ADD CONSTRAINT "experience_conversation_turns_prose_doc_id_fkey" FOREIGN KEY ("prose_doc_id") REFERENCES "public"."experience_prose_docs"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."experience_optimized_docs"
    ADD CONSTRAINT "experience_optimized_docs_prose_doc_id_fkey" FOREIGN KEY ("prose_doc_id") REFERENCES "public"."experience_prose_docs"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."analyses"
    ADD CONSTRAINT "job_analyses_job_posting_id_fkey" FOREIGN KEY ("job_posting_id") REFERENCES "public"."jobs"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."analyses"
    ADD CONSTRAINT "job_analyses_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."job_feedback"
    ADD CONSTRAINT "job_feedback_job_posting_id_fkey" FOREIGN KEY ("job_posting_id") REFERENCES "public"."jobs"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."job_feedback"
    ADD CONSTRAINT "job_feedback_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."notifications_sent"
    ADD CONSTRAINT "job_notification_sent_job_posting_id_fkey" FOREIGN KEY ("job_posting_id") REFERENCES "public"."jobs"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."notifications_sent"
    ADD CONSTRAINT "job_notification_sent_user_profile_id_fkey" FOREIGN KEY ("user_profile_id") REFERENCES "public"."user_profiles"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."jobs"
    ADD CONSTRAINT "job_postings_llm_analysis_id_fkey" FOREIGN KEY ("llm_analysis_id") REFERENCES "public"."analyses"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."jobs"
    ADD CONSTRAINT "job_postings_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "public"."sources"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."jobs"
    ADD CONSTRAINT "job_postings_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."status_log"
    ADD CONSTRAINT "job_status_log_posting_id_fkey" FOREIGN KEY ("posting_id") REFERENCES "public"."jobs"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."scores"
    ADD CONSTRAINT "job_target_scores_job_posting_id_fkey" FOREIGN KEY ("job_posting_id") REFERENCES "public"."jobs"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."scores"
    ADD CONSTRAINT "job_target_scores_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."source_discoveries"
    ADD CONSTRAINT "source_discoveries_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."document_versions"
    ADD CONSTRAINT "tailored_resume_versions_resume_id_fkey" FOREIGN KEY ("resume_id") REFERENCES "public"."documents"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."documents"
    ADD CONSTRAINT "tailored_resumes_job_posting_id_fkey" FOREIGN KEY ("job_posting_id") REFERENCES "public"."jobs"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."documents"
    ADD CONSTRAINT "tailored_resumes_source_resume_id_fkey" FOREIGN KEY ("source_resume_id") REFERENCES "public"."documents"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."target_learning_log"
    ADD CONSTRAINT "target_learning_log_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."reference_jds"
    ADD CONSTRAINT "target_reference_jds_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."uploaded_resumes"
    ADD CONSTRAINT "uploaded_resumes_prose_doc_id_fkey" FOREIGN KEY ("prose_doc_id") REFERENCES "public"."experience_prose_docs"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."user_targets"
    ADD CONSTRAINT "user_targets_target_id_fkey" FOREIGN KEY ("target_id") REFERENCES "public"."targets"("id") ON DELETE CASCADE;



CREATE POLICY "Users access document_versions for their own documents" ON "public"."document_versions" TO "authenticated" USING (("resume_id" IN ( SELECT "documents"."id"
   FROM "public"."documents"
  WHERE ("documents"."user_id" = ( SELECT "auth"."uid"() AS "uid"))))) WITH CHECK (("resume_id" IN ( SELECT "documents"."id"
   FROM "public"."documents"
  WHERE ("documents"."user_id" = ( SELECT "auth"."uid"() AS "uid")))));



CREATE POLICY "Users access experience_chunks for their own docs" ON "public"."experience_chunks" TO "authenticated" USING (("optimized_doc_id" IN ( SELECT "experience_optimized_docs"."id"
   FROM "public"."experience_optimized_docs"
  WHERE ("experience_optimized_docs"."user_id" = ( SELECT "auth"."uid"() AS "uid"))))) WITH CHECK (("optimized_doc_id" IN ( SELECT "experience_optimized_docs"."id"
   FROM "public"."experience_optimized_docs"
  WHERE ("experience_optimized_docs"."user_id" = ( SELECT "auth"."uid"() AS "uid")))));



CREATE POLICY "Users access their own documents" ON "public"."documents" TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id")) WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users access their own experience_conversation_turns" ON "public"."experience_conversation_turns" TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id")) WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users access their own experience_optimized_docs" ON "public"."experience_optimized_docs" TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id")) WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users access their own experience_preferences" ON "public"."experience_preferences" TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id")) WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users access their own experience_prose_docs" ON "public"."experience_prose_docs" TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id")) WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users access their own profile" ON "public"."user_profiles" TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id")) WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users access their own uploaded_resumes" ON "public"."uploaded_resumes" TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id")) WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users access their own user_targets" ON "public"."user_targets" TO "authenticated" USING (((( SELECT "auth"."uid"() AS "uid"))::"text" = "user_id")) WITH CHECK (((( SELECT "auth"."uid"() AS "uid"))::"text" = "user_id"));



CREATE POLICY "Users read their own analyses" ON "public"."analyses" FOR SELECT TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users read their own batch_runs" ON "public"."batch_runs" FOR SELECT TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



CREATE POLICY "Users read their own llm_costs" ON "public"."llm_costs" FOR SELECT TO "authenticated" USING ((( SELECT "auth"."uid"() AS "uid") = "user_id"));



ALTER TABLE "public"."analyses" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."batch_runs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."document_versions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."documents" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."experience_chunks" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."experience_conversation_turns" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."experience_optimized_docs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."experience_preferences" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."experience_prose_docs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."job_feedback" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "job_feedback_self_delete" ON "public"."job_feedback" FOR DELETE USING ((( SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));



CREATE POLICY "job_feedback_self_insert" ON "public"."job_feedback" FOR INSERT WITH CHECK ((( SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));



CREATE POLICY "job_feedback_self_select" ON "public"."job_feedback" FOR SELECT USING ((( SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));



CREATE POLICY "job_feedback_self_update" ON "public"."job_feedback" FOR UPDATE USING ((( SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));



ALTER TABLE "public"."jobs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."llm_costs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."notifications_sent" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."reference_jds" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."scores" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."source_discoveries" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."sources" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."status_log" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."target_derive_jd_cache" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."target_learning_log" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "target_learning_log_self_select" ON "public"."target_learning_log" FOR SELECT USING ((( SELECT ("auth"."jwt"() ->> 'sub'::"text")) = "user_id"));



ALTER TABLE "public"."targets" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."uploaded_resumes" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."user_profiles" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."user_targets" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."wyrdfold_beta_invites" ENABLE ROW LEVEL SECURITY;




ALTER PUBLICATION "supabase_realtime" OWNER TO "postgres";


GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";
GRANT USAGE ON SCHEMA "public" TO "supabase_auth_admin";









































































































































































































































































































































































































































































































































































































GRANT ALL ON FUNCTION "public"."bulk_update_recency_scores"("p_updates" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."bulk_update_recency_scores"("p_updates" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."bulk_update_recency_scores"("p_updates" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "public"."bulk_update_salaries"("p_updates" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."bulk_update_salaries"("p_updates" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."bulk_update_salaries"("p_updates" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."bulk_update_salaries"("p_updates" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "public"."bulk_update_scores"("p_updates" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."bulk_update_scores"("p_updates" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."bulk_update_scores"("p_updates" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."bulk_update_scores"("p_updates" "jsonb") TO "service_role";



GRANT ALL ON FUNCTION "public"."get_target_jobs"("p_target_id" "uuid", "p_min_score" integer, "p_status" "text", "p_company" "text", "p_search" "text", "p_sort" "text", "p_ascending" boolean, "p_limit" integer, "p_offset" integer) TO "anon";
GRANT ALL ON FUNCTION "public"."get_target_jobs"("p_target_id" "uuid", "p_min_score" integer, "p_status" "text", "p_company" "text", "p_search" "text", "p_sort" "text", "p_ascending" boolean, "p_limit" integer, "p_offset" integer) TO "authenticated";
GRANT ALL ON FUNCTION "public"."get_target_jobs"("p_target_id" "uuid", "p_min_score" integer, "p_status" "text", "p_company" "text", "p_search" "text", "p_sort" "text", "p_ascending" boolean, "p_limit" integer, "p_offset" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") TO "service_role";
GRANT ALL ON FUNCTION "public"."hook_restrict_wyrdfold_beta"("event" "jsonb") TO "supabase_auth_admin";



GRANT ALL ON FUNCTION "public"."insert_source_if_not_exists"("p_provider" "text", "p_board_token" "text", "p_company_name" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."insert_source_if_not_exists"("p_provider" "text", "p_board_token" "text", "p_company_name" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."insert_source_if_not_exists"("p_provider" "text", "p_board_token" "text", "p_company_name" "text") TO "service_role";



GRANT ALL ON TABLE "public"."targets" TO "anon";
GRANT ALL ON TABLE "public"."targets" TO "authenticated";
GRANT ALL ON TABLE "public"."targets" TO "service_role";



GRANT ALL ON FUNCTION "public"."match_target_by_label"("query_label" "text", "threshold" double precision) TO "anon";
GRANT ALL ON FUNCTION "public"."match_target_by_label"("query_label" "text", "threshold" double precision) TO "authenticated";
GRANT ALL ON FUNCTION "public"."match_target_by_label"("query_label" "text", "threshold" double precision) TO "service_role";



GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "anon";
GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "service_role";



GRANT ALL ON FUNCTION "public"."set_job_feedback_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."set_job_feedback_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."set_job_feedback_updated_at"() TO "service_role";



GRANT ALL ON FUNCTION "public"."set_target_learning_log_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."set_target_learning_log_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."set_target_learning_log_updated_at"() TO "service_role";



GRANT ALL ON FUNCTION "public"."set_user_profiles_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."set_user_profiles_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."set_user_profiles_updated_at"() TO "service_role";



GRANT ALL ON FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "anon";
GRANT ALL ON FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "authenticated";
GRANT ALL ON FUNCTION "public"."spend_by_purpose_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "service_role";



GRANT ALL ON FUNCTION "public"."sync_target_active"() TO "anon";
GRANT ALL ON FUNCTION "public"."sync_target_active"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."sync_target_active"() TO "service_role";



GRANT ALL ON FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "anon";
GRANT ALL ON FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "authenticated";
GRANT ALL ON FUNCTION "public"."total_spend_since"("p_user_id" "uuid", "p_since" timestamp with time zone) TO "service_role";






























GRANT ALL ON TABLE "public"."analyses" TO "anon";
GRANT ALL ON TABLE "public"."analyses" TO "authenticated";
GRANT ALL ON TABLE "public"."analyses" TO "service_role";



GRANT ALL ON TABLE "public"."batch_runs" TO "anon";
GRANT ALL ON TABLE "public"."batch_runs" TO "authenticated";
GRANT ALL ON TABLE "public"."batch_runs" TO "service_role";



GRANT ALL ON TABLE "public"."document_versions" TO "anon";
GRANT ALL ON TABLE "public"."document_versions" TO "authenticated";
GRANT ALL ON TABLE "public"."document_versions" TO "service_role";



GRANT ALL ON TABLE "public"."documents" TO "anon";
GRANT ALL ON TABLE "public"."documents" TO "authenticated";
GRANT ALL ON TABLE "public"."documents" TO "service_role";



GRANT ALL ON TABLE "public"."experience_chunks" TO "anon";
GRANT ALL ON TABLE "public"."experience_chunks" TO "authenticated";
GRANT ALL ON TABLE "public"."experience_chunks" TO "service_role";



GRANT ALL ON TABLE "public"."experience_conversation_turns" TO "anon";
GRANT ALL ON TABLE "public"."experience_conversation_turns" TO "authenticated";
GRANT ALL ON TABLE "public"."experience_conversation_turns" TO "service_role";



GRANT ALL ON TABLE "public"."experience_optimized_docs" TO "anon";
GRANT ALL ON TABLE "public"."experience_optimized_docs" TO "authenticated";
GRANT ALL ON TABLE "public"."experience_optimized_docs" TO "service_role";



GRANT ALL ON TABLE "public"."experience_preferences" TO "anon";
GRANT ALL ON TABLE "public"."experience_preferences" TO "authenticated";
GRANT ALL ON TABLE "public"."experience_preferences" TO "service_role";



GRANT ALL ON TABLE "public"."experience_prose_docs" TO "anon";
GRANT ALL ON TABLE "public"."experience_prose_docs" TO "authenticated";
GRANT ALL ON TABLE "public"."experience_prose_docs" TO "service_role";



GRANT ALL ON TABLE "public"."job_feedback" TO "anon";
GRANT ALL ON TABLE "public"."job_feedback" TO "authenticated";
GRANT ALL ON TABLE "public"."job_feedback" TO "service_role";



GRANT ALL ON TABLE "public"."jobs" TO "anon";
GRANT ALL ON TABLE "public"."jobs" TO "authenticated";
GRANT ALL ON TABLE "public"."jobs" TO "service_role";



GRANT ALL ON TABLE "public"."llm_costs" TO "anon";
GRANT ALL ON TABLE "public"."llm_costs" TO "authenticated";
GRANT ALL ON TABLE "public"."llm_costs" TO "service_role";



GRANT ALL ON TABLE "public"."notifications_sent" TO "anon";
GRANT ALL ON TABLE "public"."notifications_sent" TO "authenticated";
GRANT ALL ON TABLE "public"."notifications_sent" TO "service_role";



GRANT ALL ON TABLE "public"."reference_jds" TO "anon";
GRANT ALL ON TABLE "public"."reference_jds" TO "authenticated";
GRANT ALL ON TABLE "public"."reference_jds" TO "service_role";



GRANT ALL ON TABLE "public"."scores" TO "anon";
GRANT ALL ON TABLE "public"."scores" TO "authenticated";
GRANT ALL ON TABLE "public"."scores" TO "service_role";



GRANT ALL ON TABLE "public"."source_discoveries" TO "anon";
GRANT ALL ON TABLE "public"."source_discoveries" TO "authenticated";
GRANT ALL ON TABLE "public"."source_discoveries" TO "service_role";



GRANT ALL ON TABLE "public"."sources" TO "anon";
GRANT ALL ON TABLE "public"."sources" TO "authenticated";
GRANT ALL ON TABLE "public"."sources" TO "service_role";



GRANT ALL ON TABLE "public"."status_log" TO "anon";
GRANT ALL ON TABLE "public"."status_log" TO "authenticated";
GRANT ALL ON TABLE "public"."status_log" TO "service_role";



GRANT ALL ON TABLE "public"."target_derive_jd_cache" TO "anon";
GRANT ALL ON TABLE "public"."target_derive_jd_cache" TO "authenticated";
GRANT ALL ON TABLE "public"."target_derive_jd_cache" TO "service_role";



GRANT ALL ON TABLE "public"."target_learning_log" TO "anon";
GRANT ALL ON TABLE "public"."target_learning_log" TO "authenticated";
GRANT ALL ON TABLE "public"."target_learning_log" TO "service_role";



GRANT ALL ON TABLE "public"."uploaded_resumes" TO "anon";
GRANT ALL ON TABLE "public"."uploaded_resumes" TO "authenticated";
GRANT ALL ON TABLE "public"."uploaded_resumes" TO "service_role";



GRANT ALL ON TABLE "public"."user_profiles" TO "anon";
GRANT ALL ON TABLE "public"."user_profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."user_profiles" TO "service_role";



GRANT ALL ON TABLE "public"."user_targets" TO "anon";
GRANT ALL ON TABLE "public"."user_targets" TO "authenticated";
GRANT ALL ON TABLE "public"."user_targets" TO "service_role";



GRANT ALL ON TABLE "public"."wyrdfold_beta_invites" TO "anon";
GRANT ALL ON TABLE "public"."wyrdfold_beta_invites" TO "authenticated";
GRANT ALL ON TABLE "public"."wyrdfold_beta_invites" TO "service_role";
GRANT ALL ON TABLE "public"."wyrdfold_beta_invites" TO "supabase_auth_admin";









ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";



































drop extension if exists "pg_net";


