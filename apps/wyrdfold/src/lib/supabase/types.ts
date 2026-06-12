export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[];

export type Database = {
  // Allows to automatically instantiate createClient with right options
  // instead of createClient<Database, { PostgrestVersion: 'XX' }>(URL, KEY)
  __InternalSupabase: {
    PostgrestVersion: '14.5';
  };
  graphql_public: {
    Tables: {
      [_ in never]: never;
    };
    Views: {
      [_ in never]: never;
    };
    Functions: {
      graphql: {
        Args: {
          extensions?: Json;
          operationName?: string;
          query?: string;
          variables?: Json;
        };
        Returns: Json;
      };
    };
    Enums: {
      [_ in never]: never;
    };
    CompositeTypes: {
      [_ in never]: never;
    };
  };
  public: {
    Tables: {
      analyses: {
        Row: {
          cost_usd: number;
          created_at: string;
          id: string;
          job_posting_id: string;
          latency_ms: number;
          model: string;
          optimized_doc_id: string | null;
          recommendation: string;
          scorecard: Json;
          target_id: string;
          user_id: string | null;
        };
        Insert: {
          cost_usd?: number;
          created_at?: string;
          id?: string;
          job_posting_id: string;
          latency_ms?: number;
          model: string;
          optimized_doc_id?: string | null;
          recommendation: string;
          scorecard: Json;
          target_id: string;
          user_id?: string | null;
        };
        Update: {
          cost_usd?: number;
          created_at?: string;
          id?: string;
          job_posting_id?: string;
          latency_ms?: number;
          model?: string;
          optimized_doc_id?: string | null;
          recommendation?: string;
          scorecard?: Json;
          target_id?: string;
          user_id?: string | null;
        };
        Relationships: [
          {
            foreignKeyName: 'job_analyses_job_posting_id_fkey';
            columns: ['job_posting_id'];
            isOneToOne: false;
            referencedRelation: 'jobs';
            referencedColumns: ['id'];
          },
          {
            foreignKeyName: 'job_analyses_target_id_fkey';
            columns: ['target_id'];
            isOneToOne: false;
            referencedRelation: 'targets';
            referencedColumns: ['id'];
          },
        ];
      };
      batch_runs: {
        Row: {
          completed: number;
          created_at: string;
          failed: number;
          id: string;
          items: Json;
          status: string;
          total: number;
          updated_at: string;
          user_id: string | null;
        };
        Insert: {
          completed?: number;
          created_at?: string;
          failed?: number;
          id?: string;
          items?: Json;
          status?: string;
          total: number;
          updated_at?: string;
          user_id?: string | null;
        };
        Update: {
          completed?: number;
          created_at?: string;
          failed?: number;
          id?: string;
          items?: Json;
          status?: string;
          total?: number;
          updated_at?: string;
          user_id?: string | null;
        };
        Relationships: [];
      };
      document_versions: {
        Row: {
          created_at: string;
          id: string;
          payload: Json;
          payload_md: string | null;
          resume_id: string;
          source: string;
        };
        Insert: {
          created_at?: string;
          id?: string;
          payload: Json;
          payload_md?: string | null;
          resume_id: string;
          source: string;
        };
        Update: {
          created_at?: string;
          id?: string;
          payload?: Json;
          payload_md?: string | null;
          resume_id?: string;
          source?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'tailored_resume_versions_resume_id_fkey';
            columns: ['resume_id'];
            isOneToOne: false;
            referencedRelation: 'documents';
            referencedColumns: ['id'];
          },
        ];
      };
      documents: {
        Row: {
          approved_at: string | null;
          cost_usd: number | null;
          created_at: string;
          document_type: string;
          docx_payload_md_hash: string | null;
          id: string;
          input_tokens: number | null;
          jd_snapshot: string;
          jd_snapshot_hash: string;
          job_posting_id: string | null;
          latency_ms: number | null;
          model: string | null;
          output_tokens: number | null;
          payload: Json;
          payload_md: string | null;
          resume_type: string;
          source_resume_id: string | null;
          storage_path: string | null;
          style_settings: Json | null;
          updated_at: string | null;
          user_id: string | null;
          warnings: Json;
        };
        Insert: {
          approved_at?: string | null;
          cost_usd?: number | null;
          created_at?: string;
          document_type?: string;
          docx_payload_md_hash?: string | null;
          id?: string;
          input_tokens?: number | null;
          jd_snapshot: string;
          jd_snapshot_hash: string;
          job_posting_id?: string | null;
          latency_ms?: number | null;
          model?: string | null;
          output_tokens?: number | null;
          payload: Json;
          payload_md?: string | null;
          resume_type: string;
          source_resume_id?: string | null;
          storage_path?: string | null;
          style_settings?: Json | null;
          updated_at?: string | null;
          user_id?: string | null;
          warnings?: Json;
        };
        Update: {
          approved_at?: string | null;
          cost_usd?: number | null;
          created_at?: string;
          document_type?: string;
          docx_payload_md_hash?: string | null;
          id?: string;
          input_tokens?: number | null;
          jd_snapshot?: string;
          jd_snapshot_hash?: string;
          job_posting_id?: string | null;
          latency_ms?: number | null;
          model?: string | null;
          output_tokens?: number | null;
          payload?: Json;
          payload_md?: string | null;
          resume_type?: string;
          source_resume_id?: string | null;
          storage_path?: string | null;
          style_settings?: Json | null;
          updated_at?: string | null;
          user_id?: string | null;
          warnings?: Json;
        };
        Relationships: [
          {
            foreignKeyName: 'tailored_resumes_job_posting_id_fkey';
            columns: ['job_posting_id'];
            isOneToOne: false;
            referencedRelation: 'jobs';
            referencedColumns: ['id'];
          },
          {
            foreignKeyName: 'tailored_resumes_source_resume_id_fkey';
            columns: ['source_resume_id'];
            isOneToOne: false;
            referencedRelation: 'documents';
            referencedColumns: ['id'];
          },
        ];
      };
      experience_chunks: {
        Row: {
          chunk_ref: string;
          chunk_type: string;
          content: string;
          created_at: string;
          embedding: string | null;
          id: string;
          metadata: Json;
          optimized_doc_id: string;
        };
        Insert: {
          chunk_ref: string;
          chunk_type: string;
          content: string;
          created_at?: string;
          embedding?: string | null;
          id?: string;
          metadata?: Json;
          optimized_doc_id: string;
        };
        Update: {
          chunk_ref?: string;
          chunk_type?: string;
          content?: string;
          created_at?: string;
          embedding?: string | null;
          id?: string;
          metadata?: Json;
          optimized_doc_id?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'experience_chunks_optimized_doc_id_fkey';
            columns: ['optimized_doc_id'];
            isOneToOne: false;
            referencedRelation: 'experience_optimized_docs';
            referencedColumns: ['id'];
          },
        ];
      };
      experience_conversation_turns: {
        Row: {
          content: string;
          conversation_type: string;
          created_at: string;
          id: string;
          metadata: Json;
          prose_doc_id: string | null;
          role: string;
          skipped: boolean;
          turn_index: number;
          user_id: string | null;
        };
        Insert: {
          content: string;
          conversation_type: string;
          created_at?: string;
          id?: string;
          metadata?: Json;
          prose_doc_id?: string | null;
          role: string;
          skipped?: boolean;
          turn_index: number;
          user_id?: string | null;
        };
        Update: {
          content?: string;
          conversation_type?: string;
          created_at?: string;
          id?: string;
          metadata?: Json;
          prose_doc_id?: string | null;
          role?: string;
          skipped?: boolean;
          turn_index?: number;
          user_id?: string | null;
        };
        Relationships: [
          {
            foreignKeyName: 'experience_conversation_turns_prose_doc_id_fkey';
            columns: ['prose_doc_id'];
            isOneToOne: false;
            referencedRelation: 'experience_prose_docs';
            referencedColumns: ['id'];
          },
        ];
      };
      experience_optimized_docs: {
        Row: {
          created_at: string;
          id: string;
          markdown_view: string | null;
          payload: Json;
          prose_doc_id: string | null;
          source: string;
          user_id: string | null;
          version: number;
        };
        Insert: {
          created_at?: string;
          id?: string;
          markdown_view?: string | null;
          payload: Json;
          prose_doc_id?: string | null;
          source?: string;
          user_id?: string | null;
          version: number;
        };
        Update: {
          created_at?: string;
          id?: string;
          markdown_view?: string | null;
          payload?: Json;
          prose_doc_id?: string | null;
          source?: string;
          user_id?: string | null;
          version?: number;
        };
        Relationships: [
          {
            foreignKeyName: 'experience_optimized_docs_prose_doc_id_fkey';
            columns: ['prose_doc_id'];
            isOneToOne: false;
            referencedRelation: 'experience_prose_docs';
            referencedColumns: ['id'];
          },
        ];
      };
      experience_preferences: {
        Row: {
          created_at: string;
          id: string;
          payload: Json;
          updated_at: string;
          user_id: string | null;
        };
        Insert: {
          created_at?: string;
          id?: string;
          payload?: Json;
          updated_at?: string;
          user_id?: string | null;
        };
        Update: {
          created_at?: string;
          id?: string;
          payload?: Json;
          updated_at?: string;
          user_id?: string | null;
        };
        Relationships: [];
      };
      experience_prose_docs: {
        Row: {
          content: string;
          created_at: string;
          id: string;
          user_id: string | null;
          version: number;
        };
        Insert: {
          content: string;
          created_at?: string;
          id?: string;
          user_id?: string | null;
          version: number;
        };
        Update: {
          content?: string;
          created_at?: string;
          id?: string;
          user_id?: string | null;
          version?: number;
        };
        Relationships: [];
      };
      job_feedback: {
        Row: {
          applied_at: string | null;
          applied_run_id: string | null;
          created_at: string;
          id: string;
          job_posting_id: string;
          reason: string | null;
          signal: string;
          target_id: string;
          updated_at: string;
          user_id: string;
        };
        Insert: {
          applied_at?: string | null;
          applied_run_id?: string | null;
          created_at?: string;
          id?: string;
          job_posting_id: string;
          reason?: string | null;
          signal: string;
          target_id: string;
          updated_at?: string;
          user_id: string;
        };
        Update: {
          applied_at?: string | null;
          applied_run_id?: string | null;
          created_at?: string;
          id?: string;
          job_posting_id?: string;
          reason?: string | null;
          signal?: string;
          target_id?: string;
          updated_at?: string;
          user_id?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'job_feedback_job_posting_id_fkey';
            columns: ['job_posting_id'];
            isOneToOne: false;
            referencedRelation: 'jobs';
            referencedColumns: ['id'];
          },
          {
            foreignKeyName: 'job_feedback_target_id_fkey';
            columns: ['target_id'];
            isOneToOne: false;
            referencedRelation: 'targets';
            referencedColumns: ['id'];
          },
        ];
      };
      jobs: {
        Row: {
          absolute_url: string | null;
          company_name: string;
          created_at: string | null;
          department: string | null;
          description_html: string | null;
          external_id: string;
          first_seen_at: string | null;
          greenhouse_updated_at: string | null;
          id: string;
          last_url_check_at: string | null;
          llm_analysis_id: string | null;
          llm_score: number | null;
          location: string | null;
          salary_text: string | null;
          score: number;
          score_breakdown: Json | null;
          source_id: string;
          status: string;
          target_id: string | null;
          title: string;
          updated_at: string | null;
          url_check_failure_count: number;
          url_check_status: number | null;
          url_validation_status: string | null;
          url_validation_warnings: Json | null;
        };
        Insert: {
          absolute_url?: string | null;
          company_name: string;
          created_at?: string | null;
          department?: string | null;
          description_html?: string | null;
          external_id: string;
          first_seen_at?: string | null;
          greenhouse_updated_at?: string | null;
          id?: string;
          last_url_check_at?: string | null;
          llm_analysis_id?: string | null;
          llm_score?: number | null;
          location?: string | null;
          salary_text?: string | null;
          score?: number;
          score_breakdown?: Json | null;
          source_id: string;
          status?: string;
          target_id?: string | null;
          title: string;
          updated_at?: string | null;
          url_check_failure_count?: number;
          url_check_status?: number | null;
          url_validation_status?: string | null;
          url_validation_warnings?: Json | null;
        };
        Update: {
          absolute_url?: string | null;
          company_name?: string;
          created_at?: string | null;
          department?: string | null;
          description_html?: string | null;
          external_id?: string;
          first_seen_at?: string | null;
          greenhouse_updated_at?: string | null;
          id?: string;
          last_url_check_at?: string | null;
          llm_analysis_id?: string | null;
          llm_score?: number | null;
          location?: string | null;
          salary_text?: string | null;
          score?: number;
          score_breakdown?: Json | null;
          source_id?: string;
          status?: string;
          target_id?: string | null;
          title?: string;
          updated_at?: string | null;
          url_check_failure_count?: number;
          url_check_status?: number | null;
          url_validation_status?: string | null;
          url_validation_warnings?: Json | null;
        };
        Relationships: [
          {
            foreignKeyName: 'job_postings_llm_analysis_id_fkey';
            columns: ['llm_analysis_id'];
            isOneToOne: false;
            referencedRelation: 'analyses';
            referencedColumns: ['id'];
          },
          {
            foreignKeyName: 'job_postings_source_id_fkey';
            columns: ['source_id'];
            isOneToOne: false;
            referencedRelation: 'sources';
            referencedColumns: ['id'];
          },
          {
            foreignKeyName: 'job_postings_target_id_fkey';
            columns: ['target_id'];
            isOneToOne: false;
            referencedRelation: 'targets';
            referencedColumns: ['id'];
          },
        ];
      };
      llm_costs: {
        Row: {
          cache_creation_input_tokens: number;
          cache_read_input_tokens: number;
          cost_usd: number;
          created_at: string;
          id: string;
          input_tokens: number;
          latency_ms: number;
          metadata: Json;
          model: string;
          output_tokens: number;
          purpose: string;
          user_id: string | null;
        };
        Insert: {
          cache_creation_input_tokens?: number;
          cache_read_input_tokens?: number;
          cost_usd?: number;
          created_at?: string;
          id?: string;
          input_tokens?: number;
          latency_ms?: number;
          metadata?: Json;
          model: string;
          output_tokens?: number;
          purpose: string;
          user_id?: string | null;
        };
        Update: {
          cache_creation_input_tokens?: number;
          cache_read_input_tokens?: number;
          cost_usd?: number;
          created_at?: string;
          id?: string;
          input_tokens?: number;
          latency_ms?: number;
          metadata?: Json;
          model?: string;
          output_tokens?: number;
          purpose?: string;
          user_id?: string | null;
        };
        Relationships: [];
      };
      notifications_sent: {
        Row: {
          channel: string;
          external_id: string | null;
          id: string;
          job_posting_id: string;
          score_at_send: number;
          sent_at: string;
          user_profile_id: string;
        };
        Insert: {
          channel?: string;
          external_id?: string | null;
          id?: string;
          job_posting_id: string;
          score_at_send: number;
          sent_at?: string;
          user_profile_id: string;
        };
        Update: {
          channel?: string;
          external_id?: string | null;
          id?: string;
          job_posting_id?: string;
          score_at_send?: number;
          sent_at?: string;
          user_profile_id?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'job_notification_sent_job_posting_id_fkey';
            columns: ['job_posting_id'];
            isOneToOne: false;
            referencedRelation: 'jobs';
            referencedColumns: ['id'];
          },
          {
            foreignKeyName: 'job_notification_sent_user_profile_id_fkey';
            columns: ['user_profile_id'];
            isOneToOne: false;
            referencedRelation: 'user_profiles';
            referencedColumns: ['id'];
          },
        ];
      };
      reference_jds: {
        Row: {
          created_at: string | null;
          extracted_profile: Json;
          id: string;
          jd_text: string;
          jd_url: string | null;
          target_id: string;
        };
        Insert: {
          created_at?: string | null;
          extracted_profile?: Json;
          id?: string;
          jd_text: string;
          jd_url?: string | null;
          target_id: string;
        };
        Update: {
          created_at?: string | null;
          extracted_profile?: Json;
          id?: string;
          jd_text?: string;
          jd_url?: string | null;
          target_id?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'target_reference_jds_target_id_fkey';
            columns: ['target_id'];
            isOneToOne: false;
            referencedRelation: 'targets';
            referencedColumns: ['id'];
          },
        ];
      };
      scores: {
        Row: {
          axis_scores: Json | null;
          created_at: string;
          excluded: boolean;
          fit_reasoning: string | null;
          id: string;
          job_posting_id: string;
          logistics_filters: Json | null;
          matched_keywords: string[] | null;
          phase1_confidence: number | null;
          promising: boolean | null;
          recency_score: number | null;
          score: number;
          score_breakdown: Json | null;
          scored_profile_version: number;
          scoring_status: string;
          target_id: string;
          updated_at: string;
        };
        Insert: {
          axis_scores?: Json | null;
          created_at?: string;
          excluded?: boolean;
          fit_reasoning?: string | null;
          id?: string;
          job_posting_id: string;
          logistics_filters?: Json | null;
          matched_keywords?: string[] | null;
          phase1_confidence?: number | null;
          promising?: boolean | null;
          recency_score?: number | null;
          score?: number;
          score_breakdown?: Json | null;
          scored_profile_version?: number;
          scoring_status?: string;
          target_id: string;
          updated_at?: string;
        };
        Update: {
          axis_scores?: Json | null;
          created_at?: string;
          excluded?: boolean;
          fit_reasoning?: string | null;
          id?: string;
          job_posting_id?: string;
          logistics_filters?: Json | null;
          matched_keywords?: string[] | null;
          phase1_confidence?: number | null;
          promising?: boolean | null;
          recency_score?: number | null;
          score?: number;
          score_breakdown?: Json | null;
          scored_profile_version?: number;
          scoring_status?: string;
          target_id?: string;
          updated_at?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'job_target_scores_job_posting_id_fkey';
            columns: ['job_posting_id'];
            isOneToOne: false;
            referencedRelation: 'jobs';
            referencedColumns: ['id'];
          },
          {
            foreignKeyName: 'job_target_scores_target_id_fkey';
            columns: ['target_id'];
            isOneToOne: false;
            referencedRelation: 'targets';
            referencedColumns: ['id'];
          },
        ];
      };
      source_discoveries: {
        Row: {
          ats_site_filter: string | null;
          detected_board_token: string | null;
          detected_company_name: string | null;
          detected_job_count: number | null;
          detected_provider: string | null;
          discovered_at: string;
          id: string;
          outcome: string;
          search_keyword: string;
          source_url: string;
          target_id: string | null;
        };
        Insert: {
          ats_site_filter?: string | null;
          detected_board_token?: string | null;
          detected_company_name?: string | null;
          detected_job_count?: number | null;
          detected_provider?: string | null;
          discovered_at?: string;
          id?: string;
          outcome: string;
          search_keyword: string;
          source_url: string;
          target_id?: string | null;
        };
        Update: {
          ats_site_filter?: string | null;
          detected_board_token?: string | null;
          detected_company_name?: string | null;
          detected_job_count?: number | null;
          detected_provider?: string | null;
          discovered_at?: string;
          id?: string;
          outcome?: string;
          search_keyword?: string;
          source_url?: string;
          target_id?: string | null;
        };
        Relationships: [
          {
            foreignKeyName: 'source_discoveries_target_id_fkey';
            columns: ['target_id'];
            isOneToOne: false;
            referencedRelation: 'targets';
            referencedColumns: ['id'];
          },
        ];
      };
      sources: {
        Row: {
          board_token: string;
          company_name: string;
          consecutive_failures: number;
          created_at: string | null;
          enabled: boolean | null;
          id: string;
          job_count: number | null;
          last_candidate_at: string | null;
          last_polled_at: string | null;
          poll_interval_minutes: number;
          provider: string;
        };
        Insert: {
          board_token: string;
          company_name: string;
          consecutive_failures?: number;
          created_at?: string | null;
          enabled?: boolean | null;
          id?: string;
          job_count?: number | null;
          last_candidate_at?: string | null;
          last_polled_at?: string | null;
          poll_interval_minutes?: number;
          provider?: string;
        };
        Update: {
          board_token?: string;
          company_name?: string;
          consecutive_failures?: number;
          created_at?: string | null;
          enabled?: boolean | null;
          id?: string;
          job_count?: number | null;
          last_candidate_at?: string | null;
          last_polled_at?: string | null;
          poll_interval_minutes?: number;
          provider?: string;
        };
        Relationships: [];
      };
      status_log: {
        Row: {
          created_at: string | null;
          id: string;
          new_status: string;
          note: string | null;
          old_status: string | null;
          posting_id: string;
        };
        Insert: {
          created_at?: string | null;
          id?: string;
          new_status: string;
          note?: string | null;
          old_status?: string | null;
          posting_id: string;
        };
        Update: {
          created_at?: string | null;
          id?: string;
          new_status?: string;
          note?: string | null;
          old_status?: string | null;
          posting_id?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'job_status_log_posting_id_fkey';
            columns: ['posting_id'];
            isOneToOne: false;
            referencedRelation: 'jobs';
            referencedColumns: ['id'];
          },
        ];
      };
      target_derive_jd_cache: {
        Row: {
          created_at: string;
          derived_payload: Json;
          hit_count: number;
          jd_hash: string;
          last_hit_at: string | null;
          model: string;
          prompt_version: string;
        };
        Insert: {
          created_at?: string;
          derived_payload: Json;
          hit_count?: number;
          jd_hash: string;
          last_hit_at?: string | null;
          model: string;
          prompt_version: string;
        };
        Update: {
          created_at?: string;
          derived_payload?: Json;
          hit_count?: number;
          jd_hash?: string;
          last_hit_at?: string | null;
          model?: string;
          prompt_version?: string;
        };
        Relationships: [];
      };
      target_learning_log: {
        Row: {
          applied_run_id: string | null;
          confidence: number;
          created_at: string;
          diff: Json;
          id: string;
          next_profile: Json;
          prev_profile: Json;
          rationale: string | null;
          signals_consumed: number;
          status: string;
          target_id: string;
          updated_at: string;
          user_id: string;
        };
        Insert: {
          applied_run_id?: string | null;
          confidence: number;
          created_at?: string;
          diff: Json;
          id?: string;
          next_profile: Json;
          prev_profile: Json;
          rationale?: string | null;
          signals_consumed?: number;
          status: string;
          target_id: string;
          updated_at?: string;
          user_id: string;
        };
        Update: {
          applied_run_id?: string | null;
          confidence?: number;
          created_at?: string;
          diff?: Json;
          id?: string;
          next_profile?: Json;
          prev_profile?: Json;
          rationale?: string | null;
          signals_consumed?: number;
          status?: string;
          target_id?: string;
          updated_at?: string;
          user_id?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'target_learning_log_target_id_fkey';
            columns: ['target_id'];
            isOneToOne: false;
            referencedRelation: 'targets';
            referencedColumns: ['id'];
          },
        ];
      };
      targets: {
        Row: {
          activation_status: string;
          created_at: string | null;
          description: string | null;
          domain_hints: string[] | null;
          example_promising_titles: string[];
          example_unpromising_titles: string[];
          id: string;
          is_active: boolean;
          label: string;
          normalized_label: string | null;
          profile_version: number;
          scoring_profile: Json;
          search_keywords: Json | null;
          seniority_hint: string | null;
          updated_at: string | null;
        };
        Insert: {
          activation_status?: string;
          created_at?: string | null;
          description?: string | null;
          domain_hints?: string[] | null;
          example_promising_titles?: string[];
          example_unpromising_titles?: string[];
          id?: string;
          is_active?: boolean;
          label: string;
          normalized_label?: string | null;
          profile_version?: number;
          scoring_profile?: Json;
          search_keywords?: Json | null;
          seniority_hint?: string | null;
          updated_at?: string | null;
        };
        Update: {
          activation_status?: string;
          created_at?: string | null;
          description?: string | null;
          domain_hints?: string[] | null;
          example_promising_titles?: string[];
          example_unpromising_titles?: string[];
          id?: string;
          is_active?: boolean;
          label?: string;
          normalized_label?: string | null;
          profile_version?: number;
          scoring_profile?: Json;
          search_keywords?: Json | null;
          seniority_hint?: string | null;
          updated_at?: string | null;
        };
        Relationships: [];
      };
      uploaded_resumes: {
        Row: {
          created_at: string;
          extracted_text: string;
          file_size_bytes: number;
          file_type: string;
          filename: string;
          id: string;
          page_count: number | null;
          prose_doc_id: string | null;
          storage_path: string;
          user_id: string | null;
          warnings: Json;
        };
        Insert: {
          created_at?: string;
          extracted_text: string;
          file_size_bytes: number;
          file_type: string;
          filename: string;
          id?: string;
          page_count?: number | null;
          prose_doc_id?: string | null;
          storage_path: string;
          user_id?: string | null;
          warnings?: Json;
        };
        Update: {
          created_at?: string;
          extracted_text?: string;
          file_size_bytes?: number;
          file_type?: string;
          filename?: string;
          id?: string;
          page_count?: number | null;
          prose_doc_id?: string | null;
          storage_path?: string;
          user_id?: string | null;
          warnings?: Json;
        };
        Relationships: [
          {
            foreignKeyName: 'uploaded_resumes_prose_doc_id_fkey';
            columns: ['prose_doc_id'];
            isOneToOne: false;
            referencedRelation: 'experience_prose_docs';
            referencedColumns: ['id'];
          },
        ];
      };
      user_profiles: {
        Row: {
          created_at: string;
          email: string | null;
          id: string;
          job_notifications_enabled: boolean;
          job_score_threshold: number;
          last_seen_at: string | null;
          linkedin_url: string | null;
          list_min_score: number | null;
          llm_enabled: boolean;
          llm_monthly_budget_usd: number | null;
          location: string | null;
          max_active_targets: number | null;
          name: string | null;
          onboarding_completed_at: string | null;
          onboarding_current_step: string | null;
          onboarding_path: string | null;
          phone_number: string | null;
          resume_style_settings: Json | null;
          sms_daily_limit: number;
          sms_notifications_enabled: boolean;
          sms_score_threshold: number;
          unsubscribed_at: string | null;
          updated_at: string;
          user_id: string | null;
          website_url: string | null;
        };
        Insert: {
          created_at?: string;
          email?: string | null;
          id?: string;
          job_notifications_enabled?: boolean;
          job_score_threshold?: number;
          last_seen_at?: string | null;
          linkedin_url?: string | null;
          list_min_score?: number | null;
          llm_enabled?: boolean;
          llm_monthly_budget_usd?: number | null;
          location?: string | null;
          max_active_targets?: number | null;
          name?: string | null;
          onboarding_completed_at?: string | null;
          onboarding_current_step?: string | null;
          onboarding_path?: string | null;
          phone_number?: string | null;
          resume_style_settings?: Json | null;
          sms_daily_limit?: number;
          sms_notifications_enabled?: boolean;
          sms_score_threshold?: number;
          unsubscribed_at?: string | null;
          updated_at?: string;
          user_id?: string | null;
          website_url?: string | null;
        };
        Update: {
          created_at?: string;
          email?: string | null;
          id?: string;
          job_notifications_enabled?: boolean;
          job_score_threshold?: number;
          last_seen_at?: string | null;
          linkedin_url?: string | null;
          list_min_score?: number | null;
          llm_enabled?: boolean;
          llm_monthly_budget_usd?: number | null;
          location?: string | null;
          max_active_targets?: number | null;
          name?: string | null;
          onboarding_completed_at?: string | null;
          onboarding_current_step?: string | null;
          onboarding_path?: string | null;
          phone_number?: string | null;
          resume_style_settings?: Json | null;
          sms_daily_limit?: number;
          sms_notifications_enabled?: boolean;
          sms_score_threshold?: number;
          unsubscribed_at?: string | null;
          updated_at?: string;
          user_id?: string | null;
          website_url?: string | null;
        };
        Relationships: [];
      };
      user_targets: {
        Row: {
          auto_deactivated_at: string | null;
          axis_weights: Json | null;
          axis_weights_previous: Json | null;
          created_at: string;
          fit_score: number | null;
          fit_score_reasoning: string | null;
          id: string;
          is_active: boolean;
          target_id: string;
          updated_at: string;
          user_id: string;
        };
        Insert: {
          auto_deactivated_at?: string | null;
          axis_weights?: Json | null;
          axis_weights_previous?: Json | null;
          created_at?: string;
          fit_score?: number | null;
          fit_score_reasoning?: string | null;
          id?: string;
          is_active?: boolean;
          target_id: string;
          updated_at?: string;
          user_id: string;
        };
        Update: {
          auto_deactivated_at?: string | null;
          axis_weights?: Json | null;
          axis_weights_previous?: Json | null;
          created_at?: string;
          fit_score?: number | null;
          fit_score_reasoning?: string | null;
          id?: string;
          is_active?: boolean;
          target_id?: string;
          updated_at?: string;
          user_id?: string;
        };
        Relationships: [
          {
            foreignKeyName: 'user_targets_target_id_fkey';
            columns: ['target_id'];
            isOneToOne: false;
            referencedRelation: 'targets';
            referencedColumns: ['id'];
          },
        ];
      };
      wyrdfold_beta_invites: {
        Row: {
          accepted_at: string | null;
          email: string;
          invited_at: string;
        };
        Insert: {
          accepted_at?: string | null;
          email: string;
          invited_at?: string;
        };
        Update: {
          accepted_at?: string | null;
          email?: string;
          invited_at?: string;
        };
        Relationships: [];
      };
    };
    Views: {
      [_ in never]: never;
    };
    Functions: {
      bulk_update_recency_scores: {
        Args: { p_updates: Json };
        Returns: number;
      };
      bulk_update_salaries: { Args: { p_updates: Json }; Returns: number };
      bulk_update_scores: { Args: { p_updates: Json }; Returns: number };
      get_target_jobs: {
        Args: {
          p_ascending?: boolean;
          p_company?: string;
          p_limit?: number;
          p_min_score?: number;
          p_offset?: number;
          p_search?: string;
          p_sort?: string;
          p_status?: string;
          p_target_id: string;
        };
        Returns: {
          absolute_url: string;
          company_name: string;
          created_at: string;
          department: string;
          external_id: string;
          first_seen_at: string;
          greenhouse_updated_at: string;
          id: string;
          location: string;
          salary_text: string;
          score: number;
          score_breakdown: Json;
          scoring_status: string;
          source_id: string;
          status: string;
          title: string;
          total_count: number;
        }[];
      };
      hook_restrict_wyrdfold_beta: { Args: { event: Json }; Returns: Json };
      insert_source_if_not_exists: {
        Args: {
          p_board_token: string;
          p_company_name: string;
          p_provider: string;
        };
        Returns: boolean;
      };
      match_target_by_label: {
        Args: { query_label: string; threshold?: number };
        Returns: {
          activation_status: string;
          created_at: string | null;
          description: string | null;
          domain_hints: string[] | null;
          example_promising_titles: string[];
          example_unpromising_titles: string[];
          id: string;
          is_active: boolean;
          label: string;
          normalized_label: string | null;
          profile_version: number;
          scoring_profile: Json;
          search_keywords: Json | null;
          seniority_hint: string | null;
          updated_at: string | null;
        }[];
        SetofOptions: {
          from: '*';
          to: 'targets';
          isOneToOne: false;
          isSetofReturn: true;
        };
      };
      spend_by_purpose_since: {
        Args: { p_since: string; p_user_id: string };
        Returns: Json;
      };
      total_spend_since: {
        Args: { p_since: string; p_user_id: string };
        Returns: number;
      };
    };
    Enums: {
      [_ in never]: never;
    };
    CompositeTypes: {
      [_ in never]: never;
    };
  };
};

type DatabaseWithoutInternals = Omit<Database, '__InternalSupabase'>;

type DefaultSchema = DatabaseWithoutInternals[Extract<
  keyof Database,
  'public'
>];

export type Tables<
  DefaultSchemaTableNameOrOptions extends
    | keyof (DefaultSchema['Tables'] & DefaultSchema['Views'])
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals;
  }
    ? keyof (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions['schema']]['Tables'] &
        DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions['schema']]['Views'])
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals;
}
  ? (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions['schema']]['Tables'] &
      DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions['schema']]['Views'])[TableName] extends {
      Row: infer R;
    }
    ? R
    : never
  : DefaultSchemaTableNameOrOptions extends keyof (DefaultSchema['Tables'] &
        DefaultSchema['Views'])
    ? (DefaultSchema['Tables'] &
        DefaultSchema['Views'])[DefaultSchemaTableNameOrOptions] extends {
        Row: infer R;
      }
      ? R
      : never
    : never;

export type TablesInsert<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema['Tables']
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals;
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions['schema']]['Tables']
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals;
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions['schema']]['Tables'][TableName] extends {
      Insert: infer I;
    }
    ? I
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema['Tables']
    ? DefaultSchema['Tables'][DefaultSchemaTableNameOrOptions] extends {
        Insert: infer I;
      }
      ? I
      : never
    : never;

export type TablesUpdate<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema['Tables']
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals;
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions['schema']]['Tables']
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals;
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions['schema']]['Tables'][TableName] extends {
      Update: infer U;
    }
    ? U
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema['Tables']
    ? DefaultSchema['Tables'][DefaultSchemaTableNameOrOptions] extends {
        Update: infer U;
      }
      ? U
      : never
    : never;

export type Enums<
  DefaultSchemaEnumNameOrOptions extends
    | keyof DefaultSchema['Enums']
    | { schema: keyof DatabaseWithoutInternals },
  EnumName extends DefaultSchemaEnumNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals;
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions['schema']]['Enums']
    : never = never,
> = DefaultSchemaEnumNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals;
}
  ? DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions['schema']]['Enums'][EnumName]
  : DefaultSchemaEnumNameOrOptions extends keyof DefaultSchema['Enums']
    ? DefaultSchema['Enums'][DefaultSchemaEnumNameOrOptions]
    : never;

export type CompositeTypes<
  PublicCompositeTypeNameOrOptions extends
    | keyof DefaultSchema['CompositeTypes']
    | { schema: keyof DatabaseWithoutInternals },
  CompositeTypeName extends PublicCompositeTypeNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals;
  }
    ? keyof DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions['schema']]['CompositeTypes']
    : never = never,
> = PublicCompositeTypeNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals;
}
  ? DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions['schema']]['CompositeTypes'][CompositeTypeName]
  : PublicCompositeTypeNameOrOptions extends keyof DefaultSchema['CompositeTypes']
    ? DefaultSchema['CompositeTypes'][PublicCompositeTypeNameOrOptions]
    : never;

export const Constants = {
  graphql_public: {
    Enums: {},
  },
  public: {
    Enums: {},
  },
} as const;
