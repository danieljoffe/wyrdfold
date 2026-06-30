import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string; run_id: string }> };

// POST /api/targets/[id]/learn/[run_id]/apply — accept a staged ProfilePatch
// (#79). The upstream applies the diff to the shared scoring profile, bumps
// profile_version, and re-scores; 404 if no staged run with that id is the
// caller's.
export async function POST(_request: NextRequest, { params }: Params) {
  const { id, run_id } = await params;
  return proxyToWyrdfoldAPI(`/targets/${id}/learn/${run_id}/apply`, {
    method: 'POST',
  });
}
