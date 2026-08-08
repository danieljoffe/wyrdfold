import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

/**
 * Kick off a tailored resume. Non-blocking (#656): the API hands the ~39s LLM
 * pipeline to a detached task and returns `202 {status:"running"}` at once, so
 * the default 30s proxy timeout is plenty (the long `LLM_TIMEOUT_MS` is gone —
 * same change `/api/jobs/analysis` made for #459). The client polls
 * `GET /api/jobs/tailor/by-job/{id}`.
 *
 * Still returns 200 with a record on the reuse-clone path (#504), which costs
 * no LLM call, and on the JD-only operator path that has nothing to poll.
 */
export async function POST(request: NextRequest) {
  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI('/tailor/resume', {
    method: 'POST',
    body: parsed.body,
  });
}
