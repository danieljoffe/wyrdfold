import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

/**
 * Kick off a tailored cover letter. Non-blocking (#656) — see the resume
 * route for the timeout rationale. The client polls
 * `GET /api/jobs/tailor/by-job/{id}/cover-letter`.
 */
export async function POST(request: NextRequest) {
  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI('/tailor/cover-letter', {
    method: 'POST',
    body: parsed.body,
  });
}
