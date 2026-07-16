import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// Discovery over the shared target catalog. Forwards ?q= (+ optional ?limit=)
// to the API, which validates them and returns lightweight, follow-able cards.
export async function GET(request: NextRequest) {
  return proxyToWyrdfoldAPI('/targets/search', {
    searchParams: request.nextUrl.searchParams,
  });
}
