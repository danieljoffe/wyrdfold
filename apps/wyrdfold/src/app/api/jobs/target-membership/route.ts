import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

/**
 * Target-membership for the search grid (#467 §11). Forwards
 * `{ job_posting_ids: string[] }` to the authed API's
 * `POST /jobs/target-membership`, which returns which of the caller's OWN
 * targets already contain each listing — powering the card's pipeline-state
 * badge and the detail modal's bound/unbound split.
 *
 * `proxyToWyrdfoldAPI` requires a session (401 without one), so this route is
 * logged-in only, like search itself. No LLM work, so the default (fast)
 * timeout applies. The API scopes the read to the caller's own target ids, so
 * membership never leaks across users regardless of the posted job ids.
 */
export async function POST(request: NextRequest) {
  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI('/jobs/target-membership', {
    method: 'POST',
    body: parsed.body,
  });
}
