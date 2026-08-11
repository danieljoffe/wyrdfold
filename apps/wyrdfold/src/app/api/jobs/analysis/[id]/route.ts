import { type NextRequest, NextResponse } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string }> };

function requireTargetId(request: NextRequest): string | NextResponse {
  const targetId = request.nextUrl.searchParams.get('target_id');
  if (!targetId) {
    return NextResponse.json(
      { error: 'target_id query param required' },
      { status: 400 }
    );
  }
  return targetId;
}

/**
 * Kick off (or return a cached) job-fit analysis. Non-blocking (#459): on a
 * cache miss the API runs the ~26s LLM in a detached task and returns
 * `202 {status:"running"}` at once — so the default 30s proxy timeout is
 * plenty (the long LLM_TIMEOUT_MS is gone). The client polls GET below.
 */
export async function POST(request: NextRequest, { params }: Params) {
  const { id } = await params;
  const targetId = requireTargetId(request);
  if (typeof targetId !== 'string') return targetId;

  return proxyToWyrdfoldAPI(`/analysis/${id}`, {
    method: 'POST',
    searchParams: new URLSearchParams({ target_id: targetId }),
  });
}

/**
 * Poll the state of a backgrounded analysis (#459): returns the persisted
 * record once it lands, or a `{status: running | error | idle}` marker while
 * it hasn't. Read-only — no LLM spend.
 */
export async function GET(request: NextRequest, { params }: Params) {
  const { id } = await params;
  const targetId = requireTargetId(request);
  if (typeof targetId !== 'string') return targetId;

  return proxyToWyrdfoldAPI(`/analysis/${id}`, {
    method: 'GET',
    searchParams: new URLSearchParams({ target_id: targetId }),
  });
}
