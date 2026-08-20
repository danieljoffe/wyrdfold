export const JOB_STATUSES = [
  'new',
  'saved',
  'resume_draft',
  'resume_ready',
  'applied',
  'interviewing',
  'offer',
  'rejected',
  'archived',
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

export const STATUS_DOT_CLASS: Record<JobStatus, string> = {
  new: 'bg-text-tertiary',
  saved: 'bg-info',
  resume_draft: 'bg-info',
  resume_ready: 'bg-success',
  applied: 'bg-success',
  interviewing: 'bg-warning',
  offer: 'bg-warning',
  rejected: 'bg-error',
  archived: 'bg-text-tertiary',
};

export function formatStatus(status: string): string {
  // Title-cased so dropdown options match the status pill, which was the only
  // surface capitalizing (via CSS) — "resume_draft" → "Resume Draft".
  return status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

export type ScoringStatus = 'stage1' | 'stage2' | 'complete';

// Sentinel UUID for jobs added via POST /jobs/manual.
// Mirrors `MANUAL_SOURCE_ID` in apps/wyrdfold-api/app/services/extract.py.
export const MANUAL_SOURCE_ID = '00000000-0000-4000-a000-000000000001';

/**
 * Structured logistics the Phase 2 grader extracts from the JD (#86). Filter-only
 * — never affects score, recency, or sort. Mirrors the backend
 * `app/models/logistics.py` shape.
 */
export interface LogisticsFilters {
  remote_status: 'remote' | 'hybrid' | 'onsite' | 'unspecified';
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_unit: 'year' | 'hour' | null;
  location_city: string | null;
  location_country: string | null;
}

export interface JobPosting {
  id: string;
  external_id: string;
  source_id: string;
  title: string;
  /**
   * Cleaned display form of `title` (server-side, deterministic), null when
   * the raw title is already presentable. Render via `displayTitle()` — the
   * RPC-backed list paths don't serve it yet (stage 2), so it is optional.
   */
  title_display?: string | null;
  company_name: string;
  location: string | null;
  /**
   * Structured parts parsed from `location` at ingest (#518) — compose with
   * `formatLocation()`. Optional: older cached payloads may omit them.
   */
  city?: string | null;
  state?: string | null;
  country?: string | null;
  location_remote?: boolean | null;
  absolute_url: string | null;
  score: number;
  /**
   * The UNDECAYED fit score. `score` is what the list shows and floors on —
   * fit × freshness since #665 — so for anything past the 7-day grace the two
   * differ. The detail panel needs both to show an honest chain from the
   * keyword components down to the number on the card (#650).
   * Optional: responses predating #665's projection omit it.
   */
  raw_score?: number | null;
  score_breakdown: Record<string, number> | null;
  /**
   * The fit grade's four axes (title/skills/seniority/domain, 0–100) for
   * graded rows — the breakdown that actually averages to ``score`` (#609).
   * ``null`` = graded signal absent (pending row); ``undefined`` = the
   * serving path couldn't carry the column (the RPC list paths, until R3) —
   * the detail panel lazily fetches ``/api/jobs/{id}`` to fill it in.
   */
  axis_scores?: Record<string, number> | null;
  scoring_status: ScoringStatus | undefined;
  /**
   * True when the row is not yet Sonnet-graded — ``score`` is a keyword
   * placeholder, not a real fit score (#47). The list still shows these
   * (exempt from the min-score floor), badged Pending. Mirrors
   * ``scoring_status !== 'complete'``; sent explicitly by the API.
   */
  pending?: boolean;
  /**
   * Structured logistics extracted by the Phase 2 grader (#86): remote status,
   * salary, location. Filter-only. Null/absent when the job hasn't been graded
   * since logistics extraction was enabled — chips simply don't render then.
   */
  logistics_filters?: LogisticsFilters | null;
  status: string;
  salary_text: string | null;
  /**
   * Structured salary parsed at ingest (#528) — the display path
   * (formatJobSalary) prefers these over ``salary_text``. Optional:
   * older cached payloads may omit them. Vocabulary per
   * services/job_search.py: 'yearly' | 'hourly', never guessed.
   */
  salary_min?: number | null;
  salary_max?: number | null;
  salary_currency?: string | null;
  salary_period?: 'yearly' | 'hourly' | null;
  /** Provider's posted/created date (normalized), null when the source gave
   * none — e.g. manual adds. Renamed from greenhouse_updated_at (R2). */
  source_posted_at: string | null;
  /** When the listing entered OUR catalog. Renamed from created_at; also
   * absorbed the byte-identical first_seen_at (R2). */
  cataloged_at: string;
  /** Present only on the detail GET ``/jobs/{id}`` — list responses
   *  deliberately omit it to keep the payload small. */
  description_html?: string | null;
}

export interface JobsFilterState {
  minScore: string;
  status: string;
  search: string;
  /** Comma-separated location terms to exclude. Substring match against
   *  the job's ``location`` field (case-insensitive on the API side).
   *  Empty string = no exclusion. */
  excludeLocations: string;
  /** Comma-separated location terms — show only postings whose location
   *  contains at least one of them. Empty string = no restriction. */
  onlyLocations: string;
  /** Logistics filters (#86), over the grader's `logistics_filters`.
   *  First-class dimensions like the rest: carried in the URL AND the
   *  per-target localStorage persistence (the v1 URL-only carve-out made
   *  them silently reset on any bare re-entry). Empty string = inactive.
   *  The canonical field list lives in ``jobsFilterFields.ts`` — its
   *  compile-time pins fail the build if this interface and that list
   *  ever drift. */
  remoteOnly: string; // '' | 'true'
  minSalary: string; // '' | numeric string (annual USD)
  country: string; // '' | ISO country code
}

/** Wire sort tokens.
 *
 * 'posted_at' sorts the PROVIDER'S date (``source_posted_at``) — what the
 * Posted column shows — with the listings that carry none sorted last.
 *
 * 'created_at' is kept for URL/param stability and sorts the renamed
 * cataloged_at column server-side (R2): when WE catalogued the listing, which
 * is a different question and no longer what the Posted column asks. */
export type JobsSortColumn =
  'score' | 'posted_at' | 'created_at' | 'company_name' | 'title';

interface SkillMatch {
  name: string;
  matched: boolean;
  confidence: 'high' | 'medium' | 'low';
  evidence: string | null;
}

interface Scorecard {
  skills_matched: SkillMatch[];
  skills_missing: string[];
  nice_to_haves: string[];
  seniority_fit: 'strong' | 'moderate' | 'weak';
  seniority_rationale: string;
  domain_fit: 'strong' | 'moderate' | 'weak';
  domain_rationale: string;
}

export interface JobAnalysis {
  id: string;
  job_posting_id: string;
  scorecard: Scorecard;
  recommendation: string;
  model: string;
  cost_usd: number;
  latency_ms: number;
  created_at: string;
}

/**
 * Poll marker for the non-blocking analysis flow (#459). The kick-off POST
 * returns this (202) on a cache miss, and GET returns it while the detached
 * run hasn't finished:
 *  - `running` — in flight; keep polling.
 *  - `error`   — the run failed; offer a retry.
 *  - `idle`    — nothing cached and nothing in flight (e.g. a server restart
 *    dropped the run); re-kick via POST.
 */
export interface AnalysisStatus {
  status: 'running' | 'error' | 'idle';
  message?: string;
}

// ---------------------------------------------------------------------------
// Resume lifecycle types (#505)
// ---------------------------------------------------------------------------

interface TailoredBullet {
  text: string;
  source_outcome_ref: string | null;
}

interface TailoredRole {
  company: string;
  title: string;
  location: string | null;
  start: string;
  end: string | null;
  bullets: TailoredBullet[];
  source_role_ref: string;
}

interface TailoredEducation {
  school: string;
  degree: string | null;
  dates: string | null;
}

interface ContactInfo {
  name: string;
  email: string | null;
  phone: string | null;
  location: string | null;
  website: string | null;
  linkedin: string | null;
}

interface TailoredResumePayload {
  summary: string;
  contact: ContactInfo;
  experience: TailoredRole[];
  skills: string[];
  education: TailoredEducation[];
  resume_type: string;
  jd_snippet: string;
  preferences_applied: string[];
}

export interface LintViolation {
  code: string;
  message: string;
  severity: 'error' | 'warning';
}

export interface TailoredResumeRecord {
  id: string;
  user_id: string | null;
  job_posting_id: string | null;
  document_type: 'resume' | 'cover_letter';
  resume_type: string;
  jd_snapshot: string;
  jd_snapshot_hash: string;
  payload: TailoredResumePayload | CoverLetterPayload;
  payload_md: string | null;
  docx_payload_md_hash: string | null;
  storage_path: string | null;
  warnings: string[];
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_ms: number;
  created_at: string;
  updated_at: string | null;
  approved_at: string | null;
  source_resume_id: string | null;
  /**
   * Whether this document was generated under the user's stretch opt-in — the
   * "Generate anyway" confirm on a Skip-verdict job (#780/#785). Re-generate
   * on the review page reuses it, because that route has no target in scope
   * and the Skip verdict it would need is per-(job, target).
   *
   * Optional on the type so a stale cached response still parses; the API
   * always sends it and it is `false` for every resume.
   */
  allow_stretch?: boolean;
  /**
   * ATS lint state (#656). `null` = never linted (rows predating the column);
   * `[]` = linted with nothing to report; a list with any
   * `severity: 'error'` entry = **flagged draft**,
   * persisted despite failing lint so the generation spend isn't thrown away.
   * A warnings-only list is clean-with-advisories, NOT flagged — use
   * `isFlaggedDraft()` rather than a length check.
   */
  lint_violations?: LintViolation[] | null;
}

/** True when a record's lint state marks it a flagged draft — i.e. it carries
 *  at least one blocking violation. Warnings alone don't flag a draft. */
export function isFlaggedDraft(
  record: Pick<TailoredResumeRecord, 'lint_violations'> | null | undefined
): boolean {
  return (record?.lint_violations ?? []).some(v => v.severity === 'error');
}

/**
 * Poll marker for the non-blocking tailor flow (#656), mirroring
 * `AnalysisStatus` for #459.
 *  - `running` — a detached generation is in flight; keep polling.
 *  - `error`   — the run failed; offer a retry (POST again).
 *  - `idle`    — nothing in flight. With a record that means "settled"; with
 *    a null record it's the "Generate" empty state.
 */
export type TailorRunStatus = 'running' | 'error' | 'idle';

/** Response shape of `GET /api/jobs/tailor/by-job/{id}` (and its cover-letter
 *  sibling). Replaced the bare `TailoredResumeRecord | null` those routes used
 *  to return: a client that kicked off a background run needs to tell "nothing
 *  here yet, keep polling" from "nothing here, and nothing coming". */
export interface TailoredDocumentState {
  record: TailoredResumeRecord | null;
  status: TailorRunStatus;
  message?: string | null;
}

/** 202 body from POST /api/jobs/tailor/resume | /cover-letter. */
export interface TailorStatusResponse {
  status: TailorRunStatus;
  message?: string | null;
}

/** Response shape of `POST /api/jobs/tailor/{id}/ats-recheck`. */
export interface AtsRecheckResponse {
  ok: boolean;
  violations: LintViolation[];
  record: TailoredResumeRecord;
}

interface CoverLetterParagraph {
  text: string;
}

interface CoverLetterPayload {
  contact: ContactInfo;
  recipient_company: string;
  recipient_role: string | null;
  salutation: string;
  paragraphs: CoverLetterParagraph[];
  closing: string;
  signature: string;
  jd_snippet: string;
  preferences_applied: string[];
  source_outcome_refs: string[];
  source_role_refs: string[];
  source_skill_refs: string[];
}

export interface TailorResponse {
  record: TailoredResumeRecord;
  lint_warnings: LintViolation[];
}

export interface StatusLogEntry {
  id: string;
  old_status: string | null;
  new_status: string;
  note: string | null;
  created_at: string;
}

type ResumeVersionSource = 'initial' | 'user_edit' | 'llm_adapt';

export interface ResumeVersion {
  id: string;
  resume_id: string;
  payload: TailoredResumePayload;
  /**
   * The snapshot's markdown — what a restore actually writes back. Null only
   * for rows snapshotted before this column was populated.
   *
   * Declared here rather than cast at each use site: the API omitted this
   * field entirely (its Pydantic model had no `payload_md` and
   * `extra: "ignore"` dropped the column), so every version failed restore.
   * Typing it means the next serialization gap fails the build instead of
   * silently degrading to "cannot restore".
   */
  payload_md: string | null;
  source: ResumeVersionSource;
  created_at: string;
}

/**
 * Can this snapshot actually be restored? Restore writes `payload_md` into the
 * editor, so a version without it is listable but not loadable.
 */
export function hasRestorableMarkdown(version: ResumeVersion): boolean {
  return Boolean(version.payload_md);
}

export interface ResumeVersionsResponse {
  versions: ResumeVersion[];
  cap: number;
}

/** The date a card shows as "Posted" — the PROVIDER'S own date, or null.
 *
 * Null when the ATS gave us none (~4% of live listings). It stays null rather
 * than falling back to our catalog date: "Posted" is a claim about the
 * employer's board, and quietly substituting the day WE happened to find the
 * listing misattributes our number to them. Callers render null as an em dash
 * (``timeAgo`` already does), and the Posted sort puts those rows last.
 *
 * For the freshness the SCORE was decayed against, use
 * {@link freshnessAnchorAt} — the two are different questions and the server
 * answers them differently. */
export function postedAt(job: {
  source_posted_at: string | null;
}): string | null {
  return job.source_posted_at;
}

/** The date the server's recency decay ages a score against: the provider's
 * date when known, else when we cataloged the listing (R2 two-timestamp
 * model). Mirrors ``source_posted_at or cataloged_at`` in the API's
 * ``_display_sort_value`` / ``recency.py`` — keep the two in step, or the
 * panel's "aged N days" stops explaining the number on the card. */
export function freshnessAnchorAt(job: {
  source_posted_at: string | null;
  cataloged_at: string;
}): string {
  return job.source_posted_at ?? job.cataloged_at;
}

/**
 * Does the match analysis recommend skipping this job?
 *
 * The analysis opens with its verdict ("Skip: this is a Senior UX Designer
 * role requiring…"), so the leading word carries the recommendation. Used to
 * warn before a paid generation: a "Skip" job is exactly where the model may
 * decline to apply on the user's behalf, and being charged for a refusal with
 * no warning is the worst version of that.
 *
 * Deliberately conservative — only an explicit leading Skip/Pass/Avoid counts.
 * A false positive adds friction to a good match, which is worse than missing
 * a marginal one.
 */
export function isSkipRecommendation(
  recommendation: string | null | undefined
): boolean {
  if (!recommendation) return false;
  return /^\s*(skip|pass|avoid|do not apply|don't apply)\b/i.test(
    recommendation
  );
}
