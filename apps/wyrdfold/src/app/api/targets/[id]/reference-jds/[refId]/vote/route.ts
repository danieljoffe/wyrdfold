import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string; refId: string }> };

export async function POST(request: NextRequest, { params }: Params) {
  const { id, refId } = await params;
  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI(`/targets/${id}/reference-jds/${refId}/vote`, {
    method: 'POST',
    body: parsed.body,
  });
}
