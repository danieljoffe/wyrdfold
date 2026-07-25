/**
 * Public/manual job-search shapes (#467) — mirrors the API `JobSearchResult` /
 * `JobSearchResponse`. Deliberately carries NO match score (manual search is
 * kept distinct from the AI-matched Jobs ranking) and no JD body.
 */
export interface JobSearchResult {
  id: string;
  title: string;
  company_name: string;
  location: string | null;
  department: string | null;
  salary_text: string | null;
  absolute_url: string | null;
  first_seen_at: string | null;
  created_at: string | null;
  // Short plaintext preview (tag-stripped, truncated server-side) — the triage
  // payload for the card grid (#467 §11). Null when the JD has no usable text.
  snippet: string | null;
}

export interface JobSearchResponse {
  query: string;
  count: number;
  has_more: boolean;
  results: JobSearchResult[];
}

/**
 * A target a listing already belongs to — mirrors the API's `TargetRef` from
 * `POST /jobs/target-membership`. Drives the card's pipeline-state badge and the
 * detail modal's bound/unbound split (#467 §11).
 */
export interface TargetRef {
  target_id: string;
  label: string;
}

/** `job_posting_id` → the caller's targets that already contain it (absent key /
 *  empty array = not in any of the caller's targets). */
export interface TargetMembershipResponse {
  memberships: Record<string, TargetRef[]>;
}
