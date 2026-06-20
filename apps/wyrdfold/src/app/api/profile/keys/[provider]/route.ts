import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

type Params = { params: Promise<{ provider: string }> };

// Store / replace the caller's key for `provider` (#5 P4). The plaintext is
// forwarded once and never read back; the response is non-secret metadata.
export async function PUT(request: NextRequest, { params }: Params) {
  const { provider } = await params;
  const parsed = await readJsonBody<{ key?: unknown }>(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI(`/profile/keys/${encodeURIComponent(provider)}`, {
    method: 'PUT',
    body: parsed.body,
  });
}

// Remove the caller's key for `provider`. Idempotent upstream.
export async function DELETE(_request: NextRequest, { params }: Params) {
  const { provider } = await params;
  return proxyToWyrdfoldAPI(`/profile/keys/${encodeURIComponent(provider)}`, {
    method: 'DELETE',
  });
}
