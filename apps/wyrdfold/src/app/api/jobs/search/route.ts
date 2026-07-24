import { type NextRequest, NextResponse } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

/**
 * Logged-in keyword job search (#467). `proxyToWyrdfoldAPI` requires a session
 * (401s without one), so this route is authed-only — search is gated to the
 * beta cohort while the UX + abuse controls are proven; there is no public
 * surface yet. Forwards `q` (+ optional `page_size`) to the API `GET /search`.
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

  return proxyToWyrdfoldAPI('/search', { searchParams });
}
