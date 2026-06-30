import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

interface RouteContext {
  params: Promise<{ id: string }>;
}

// GET /api/targets/[id]/learning-log — the feedback learner's audit list
// (#79). Passes through ?status=applied|staged|rejected and ?limit so the
// review UI can fetch just the staged patches or the full history.
export async function GET(request: NextRequest, ctx: RouteContext) {
  const { id } = await ctx.params;
  return proxyToWyrdfoldAPI(`/targets/${id}/learning-log`, {
    searchParams: request.nextUrl.searchParams,
  });
}
