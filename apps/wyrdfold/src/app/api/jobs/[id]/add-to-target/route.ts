import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string }> };

/**
 * Add an EXISTING posting (a search result's job id) to one of the caller's
 * targets (#467 power-action). Forwards `{ target_id }` to the authed API,
 * which scores the already-ingested posting against the chosen target and
 * saves it to the caller's pipeline — no LLM, so the default (fast) timeout
 * applies. `proxyToWyrdfoldAPI` requires a session (401 without one), so this
 * route is logged-in only, like search itself.
 */
export async function POST(request: NextRequest, { params }: Params) {
  const { id } = await params;
  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI(`/jobs/${id}/add-to-target`, {
    method: 'POST',
    body: parsed.body,
  });
}
