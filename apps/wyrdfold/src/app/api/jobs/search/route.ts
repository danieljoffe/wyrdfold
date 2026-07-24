import { type NextRequest, NextResponse } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

/**
 * Logged-in keyword job search (#467). `proxyToWyrdfoldAPI` requires a session
 * (401s without one), so this route is authed-only — search is gated to the
 * beta cohort while the UX + abuse controls are proven; there is no public
 * surface yet. Forwards `q` (+ optional `page_size`, `offset`) to `GET /search`.
 */
export async function GET(request: NextRequest) {
  const sp = request.nextUrl.searchParams;
  const q = sp.get('q')?.trim();
  if (!q) {
    return NextResponse.json(
      { error: 'q query param required' },
      { status: 400 }
    );
  }

  const searchParams = new URLSearchParams({ q });
  const pageSize = sp.get('page_size');
  if (pageSize) searchParams.set('page_size', pageSize);
  const offset = sp.get('offset');
  if (offset) searchParams.set('offset', offset);
  // Filters (#467 fast-follow): forward the location substring + recency bound
  // so the API can narrow the corpus. Missing these here silently drops the
  // filters (the API returns unfiltered results).
  const location = sp.get('location');
  if (location) searchParams.set('location', location);
  const postedWithinDays = sp.get('posted_within_days');
  if (postedWithinDays)
    searchParams.set('posted_within_days', postedWithinDays);

  return proxyToWyrdfoldAPI('/search', { searchParams });
}
