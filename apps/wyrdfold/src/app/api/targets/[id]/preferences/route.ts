import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string }> };

/**
 * GET /api/targets/{id}/preferences — the calling user's per-target,
 * read-time preferences (#60): a filter/re-rank over the SHARED cached fit
 * score (never a per-user re-grade). 404s when the user isn't linked to the
 * target.
 */
export async function GET(_request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyToWyrdfoldAPI(`/targets/${id}/preferences`, { method: 'GET' });
}

/**
 * PUT /api/targets/{id}/preferences — replace the calling user's preferences
 * (full-replace semantics: omitted fields reset to their documented defaults).
 * Pure read-time config; does not re-grade existing scores.
 */
export async function PUT(request: NextRequest, { params }: Params) {
  const { id } = await params;
  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI(`/targets/${id}/preferences`, {
    method: 'PUT',
    body: parsed.body,
  });
}
