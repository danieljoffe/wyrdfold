import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string }> };

export async function GET(_request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyToWyrdfoldAPI(`/targets/${id}`);
}

// PATCH removed (SEC-2, #366): the target row is a shared catalog — direct
// field edits are operator-only on the API. Per-user tuning goes through the
// axis-weights / preferences sub-routes; shared-model contribution through the
// bounded #191 path (reference JDs, learning log).

export async function DELETE(_request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyToWyrdfoldAPI(`/targets/${id}`, { method: 'DELETE' });
}
