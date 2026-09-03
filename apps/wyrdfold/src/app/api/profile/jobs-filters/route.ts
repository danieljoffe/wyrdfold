import type { NextRequest } from 'next/server';
import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// #866: /jobs filter memory lives server-side on the caller's own profile
// row, so a shared browser can no longer leak one account's filters into
// the next. Plain authed proxy — validation (size/key caps) is the API's.
// ``proxyToWyrdfoldAPI`` JSON-stringifies ``body`` itself, so the PUT hands
// it the parsed object (malformed JSON degrades to a null body → API 422).
export async function GET() {
  return proxyToWyrdfoldAPI('/profile/jobs-filters');
}

export async function PUT(request: NextRequest) {
  const body = (await request.json().catch(() => null)) as unknown;
  return proxyToWyrdfoldAPI('/profile/jobs-filters', { method: 'PUT', body });
}
