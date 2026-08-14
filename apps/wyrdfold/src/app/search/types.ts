/**
 * Public/manual job-search shapes (#467) — mirrors the API `JobSearchResult` /
 * `JobSearchResponse`. Deliberately carries NO match score (manual search is
 * kept distinct from the AI-matched Jobs ranking) and no JD body.
 */
export interface JobSearchResult {
  id: string;
  title: string;
  /** Cleaned display form; null when raw `title` is fine. See `displayTitle()`. */
  title_display?: string | null;
  company_name: string;
  location: string | null;
  // Structured parts parsed from `location` at ingest (#518) — compose with
  // `formatLocation()`; null when the raw string was unparseable.
  city: string | null;
  state: string | null;
  country: string | null;
  location_remote: boolean | null;
  salary_text: string | null;
  // Structured salary parsed from salary_text at ingest (query-grade; the raw
  // text stays the display value; the salary filter applies server-side).
  // Optional until the FE consumes them. period: 'yearly' | 'hourly' | null.
  salary_min?: number | null;
  salary_max?: number | null;
  salary_currency?: string | null;
  salary_period?: string | null;
  absolute_url: string | null;
  /** Provider's posted date (null = source gave none). R2 rename. */
  source_posted_at: string | null;
  /** When we cataloged the listing. R2 rename of created_at/first_seen_at. */
  cataloged_at: string | null;
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
