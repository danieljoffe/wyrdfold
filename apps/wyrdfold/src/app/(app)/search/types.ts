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
