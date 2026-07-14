import type { JobsFilterState } from './types';

/**
 * THE single definition of the /jobs filter dimensions.
 *
 * Every layer that enumerates filters derives from this list: the URL
 * codec (`useJobsUrlState`), the per-target localStorage persistence
 * (`useJobsFilterPersistence`), the API query mapping (`JobsListView`),
 * and the snapshot/restore wiring (`JobsList`). Before this module the
 * same list was hand-enumerated in four places and drifted: the
 * logistics filters (#86) reached the URL + API layers but never
 * persistence, so remote/salary/country silently reset on any bare
 * re-entry to /jobs.
 *
 * Adding a filter dimension = one entry here (+ its field on
 * `JobsFilterState` — the pins below fail the build if either side is
 * forgotten) and the UI control. URL, persistence, and API wiring come
 * for free.
 */
export const JOBS_FILTER_FIELDS = [
  { field: 'search', urlKey: 'q', apiParam: 'search' },
  { field: 'status', urlKey: 's', apiParam: 'status' },
  { field: 'minScore', urlKey: 'score', apiParam: 'min_score' },
  {
    field: 'excludeLocations',
    urlKey: 'exclude',
    apiParam: 'exclude_locations',
  },
  { field: 'onlyLocations', urlKey: 'only', apiParam: 'only_locations' },
  { field: 'remoteOnly', urlKey: 'remote_only', apiParam: 'remote_only' },
  { field: 'minSalary', urlKey: 'min_salary', apiParam: 'min_salary' },
  { field: 'country', urlKey: 'country', apiParam: 'country' },
] as const;

export type JobsFilterField = (typeof JOBS_FILTER_FIELDS)[number]['field'];

// Compile-time pins: the field list and JobsFilterState must cover each
// other exactly. Adding to one without the other fails the build here,
// not at runtime in whichever layer happened to notice.
type _FieldsCoverState = keyof JobsFilterState extends JobsFilterField
  ? true
  : never;
type _NoExtraFields = JobsFilterField extends keyof JobsFilterState
  ? true
  : never;
const _fieldsCoverState: _FieldsCoverState = true;
const _noExtraFields: _NoExtraFields = true;
void _fieldsCoverState;
void _noExtraFields;

/** A JobsFilterState with every dimension inactive. */
export function emptyFilters(): JobsFilterState {
  return Object.fromEntries(
    JOBS_FILTER_FIELDS.map(f => [f.field, ''])
  ) as Record<JobsFilterField, string>;
}

/** True when no filter dimension is active. */
export function isFilterStateEmpty(filters: JobsFilterState): boolean {
  return JOBS_FILTER_FIELDS.every(f => !filters[f.field]);
}

/** Copy just the filter dimensions out of a superset (e.g. the URL state,
 *  which also carries sort/order/target navigation fields). */
export function pickFilters(
  source: Record<JobsFilterField, string>
): JobsFilterState {
  return Object.fromEntries(
    JOBS_FILTER_FIELDS.map(f => [f.field, source[f.field] ?? ''])
  ) as Record<JobsFilterField, string>;
}

/** Filters → URL-state patch: every dimension present, empty string
 *  mapped to ``null`` so inactive dimensions leave the URL entirely. */
export function filtersToUrlPatch(
  filters: JobsFilterState
): Record<JobsFilterField, string | null> {
  return Object.fromEntries(
    JOBS_FILTER_FIELDS.map(f => [f.field, filters[f.field] || null])
  ) as Record<JobsFilterField, string | null>;
}

/** Map active filter dimensions onto the /api/jobs query params. */
export function filtersToApiParams(
  filters: JobsFilterState
): Record<string, string> {
  const params: Record<string, string> = {};
  for (const f of JOBS_FILTER_FIELDS) {
    const value = filters[f.field];
    if (value) params[f.apiParam] = value;
  }
  return params;
}

/**
 * Normalize a parsed localStorage payload into a `JobsFilterState`.
 * Returns `null` when nothing is recoverable (caller leaves the URL
 * alone). Per-field validation stays permissive — one bad field doesn't
 * poison the snapshot, and a pre-logistics v1 snapshot (5 fields) still
 * restores its 5 with the newer dimensions empty.
 */
export function coerceStoredFilters(raw: unknown): JobsFilterState | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const obj = raw as Record<string, unknown>;
  const out = Object.fromEntries(
    JOBS_FILTER_FIELDS.map(f => [
      f.field,
      typeof obj[f.field] === 'string' ? (obj[f.field] as string) : '',
    ])
  ) as Record<JobsFilterField, string>;
  return isFilterStateEmpty(out) ? null : out;
}
