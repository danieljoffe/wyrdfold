import { NextResponse } from 'next/server';

import {
  LLM_TIMEOUT_MS,
  proxyToWyrdfoldAPI,
  readJsonBody,
} from '@/lib/api/proxy';

/**
 * LLM fallback for catalog search. When `GET /targets/search` finds no existing
 * target for a free-text query, the search UI posts the query here: the API's
 * LLM canonicalises it into a target plus a few adjacent roles the user can
 * follow or create. Mirrors `/targets/suggest`'s auth/proxy/error shape, but
 * carries a `{ query }` body (the raw search text). LLM-backed → long timeout.
 */
interface SuggestFromQueryBody {
  query?: unknown;
}

// Matches the API's `SuggestFromQueryRequest` cap (mirrors `/targets/search`'s
// `q`). A cheap UX pre-check; the server re-validates and is the source of truth.
const MAX_QUERY_LENGTH = 200;

export async function POST(request: Request) {
  const parsed = await readJsonBody<SuggestFromQueryBody>(request);
  if (!parsed.ok) return parsed.response;

  const query =
    typeof parsed.body.query === 'string' ? parsed.body.query.trim() : '';
  if (!query || query.length > MAX_QUERY_LENGTH) {
    return NextResponse.json(
      { error: 'A search query of up to 200 characters is required.' },
      { status: 400 }
    );
  }

  return proxyToWyrdfoldAPI('/targets/suggest-from-query', {
    method: 'POST',
    body: { query },
    timeoutMs: LLM_TIMEOUT_MS,
  });
}
