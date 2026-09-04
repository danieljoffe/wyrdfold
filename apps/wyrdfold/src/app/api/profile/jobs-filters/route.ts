import type { NextRequest } from 'next/server';
import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// #866: /jobs filter memory lives server-side on the caller's own profile
// row, so a shared browser can no longer leak one account's filters into
// the next. Plain authed proxy — validation (size/key caps) is the API's.
// Writes are PATCH-merge (value = replace, null = delete): a patch of only
// the changed keys is safe to send before the client has ever read the
// map, which is what keeps a fast navigation from outrunning persistence.
// ``proxyToWyrdfoldAPI`` JSON-stringifies ``body`` itself, so the handler
// passes the parsed object (malformed JSON degrades to null → API 422).
export async function GET() {
  return proxyToWyrdfoldAPI('/profile/jobs-filters');
}

export async function PATCH(request: NextRequest) {
  const body = (await request.json().catch(() => null)) as unknown;
  return proxyToWyrdfoldAPI('/profile/jobs-filters', {
    method: 'PATCH',
    body,
  });
}
