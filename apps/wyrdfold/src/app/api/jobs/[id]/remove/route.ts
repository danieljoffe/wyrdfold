import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string }> };

/**
 * Remove a posting from one of the caller's targets — the honest replacement
 * for the old "Delete", which soft-archived the row and left it in the list.
 *
 * The BFF hop is easy to forget and invisible to jest (which mocks `fetch`):
 * without this file every `/api/jobs/{id}/remove` call 404s at Next.js before
 * it ever reaches the API. It is also where a request body gets silently
 * dropped if the proxy forgets to forward it, so `{ target_id }` is read and
 * passed through explicitly.
 */
export async function POST(request: NextRequest, { params }: Params) {
  const { id } = await params;
  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI(`/jobs/${id}/remove`, {
    method: 'POST',
    body: parsed.body,
  });
}

/**
 * Undo — the recourse the old flow lacked. `target_id` is optional and
 * arrives as a query param, so it is forwarded on the path rather than in a
 * body (DELETE bodies are not reliably carried end-to-end).
 */
export async function DELETE(request: NextRequest, { params }: Params) {
  const { id } = await params;
  const targetId = request.nextUrl.searchParams.get('target_id');
  const query = targetId ? `?target_id=${encodeURIComponent(targetId)}` : '';
  return proxyToWyrdfoldAPI(`/jobs/${id}/remove${query}`, { method: 'DELETE' });
}
