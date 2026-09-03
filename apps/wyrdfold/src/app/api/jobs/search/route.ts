import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';
import { SEARCH_FILTER_PARAMS } from '@/lib/api/searchFilterParams';

/**
 * Logged-in keyword job search (#467). `proxyToWyrdfoldAPI` requires a session
 * (401s without one), so this route is authed-only — search is gated to the
 * beta cohort while the UX + abuse controls are proven; there is no public
 * surface yet. Forwards `q` (+ optional `page_size`, `offset`) to `GET /search`.
 *
 * Filter params come from the shared `SEARCH_FILTER_PARAMS` list — a filter
 * missing here is silently dropped (the API returns unfiltered results), and
 * that regression shipped twice before the list existed.
 */
export async function GET(request: NextRequest) {
  const sp = request.nextUrl.searchParams;
  // A blank `q` is a valid request now: the backend browses the pool
  // newest-first (#834).
  const q = sp.get('q')?.trim() ?? '';

  const searchParams = new URLSearchParams({ q });
  const pageSize = sp.get('page_size');
  if (pageSize) searchParams.set('page_size', pageSize);
  const offset = sp.get('offset');
  if (offset) searchParams.set('offset', offset);
  for (const param of SEARCH_FILTER_PARAMS) {
    const value = sp.get(param);
    if (value) searchParams.set(param, value);
  }

  return proxyToWyrdfoldAPI('/search', { searchParams });
}
