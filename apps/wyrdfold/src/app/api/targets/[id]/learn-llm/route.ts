import type { NextRequest } from 'next/server';

import { LLM_TIMEOUT_MS, proxyToWyrdfoldAPI } from '@/lib/api/proxy';

interface RouteContext {
  params: Promise<{ id: string }>;
}

// POST /api/targets/[id]/learn-llm — force-run the LLM ProfilePatch learner
// over the target's recent feedback (#79). LLM-backed, so it uses the longer
// timeout and is subject to the upstream's per-user LLM budget (the upstream
// returns a structured error the UI surfaces via extractApiError). Returns
// null when there's nothing above threshold to learn from.
export async function POST(_request: NextRequest, ctx: RouteContext) {
  const { id } = await ctx.params;
  return proxyToWyrdfoldAPI(`/targets/${id}/learn-llm`, {
    method: 'POST',
    timeoutMs: LLM_TIMEOUT_MS,
  });
}
