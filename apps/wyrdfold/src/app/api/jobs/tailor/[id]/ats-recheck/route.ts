import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string }> };

/**
 * Re-run ATS lint over a draft's saved markdown and refresh its
 * `lint_violations` (#656). Deterministic — no LLM, no cost, no daily cap —
 * which is the point: a resume that failed lint is now kept as a flagged
 * draft, so the user fixes it and re-checks instead of paying to regenerate.
 */
export async function POST(_request: NextRequest, { params }: Params) {
  const { id } = await params;
  return proxyToWyrdfoldAPI(`/tailor/resumes/${id}/ats-recheck`, {
    method: 'POST',
  });
}
