/**
 * THE single list of search *filter* params both search BFF routes forward
 * upstream — the authed proxy (`/api/jobs/search` → `/search`) and the public
 * forwarder (`/api/public/search` → `/public/search`).
 *
 * This list exists because the same regression shipped TWICE: a filter param
 * added to the API + component reached one BFF route but not the other, and
 * the missing side silently returned unfiltered results (#467 fast-follow
 * dropped location/recency on the authed route; the salary filter launch
 * dropped `salary_floor` there too — caught in the release-gate prod smoke).
 * Adding a filter = one entry here; both routes and both route specs pick it
 * up. Core params (`q`, `page_size`, `offset`) stay per-route — their
 * validation differs by audience.
 */
export const SEARCH_FILTER_PARAMS = [
  'location',
  'posted_within_days',
  'salary_floor',
] as const;
