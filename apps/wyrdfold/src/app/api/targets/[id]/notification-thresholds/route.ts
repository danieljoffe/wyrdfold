import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string }> };

/**
 * PATCH /api/targets/{id}/notification-thresholds — set this target's
 * per-channel email/SMS alert thresholds (#15).
 *
 * Partial update: only the channels present in the body are written, so
 * editing one never clobbers the other. An explicit `null` resets that
 * channel to the account-wide default (`user_profiles.{job,sms}_score_threshold`);
 * an omitted channel is left untouched. Returns the updated UserTarget row.
 */
export async function PATCH(request: NextRequest, { params }: Params) {
  const { id } = await params;
  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI(`/targets/${id}/notification-thresholds`, {
    method: 'PATCH',
    body: parsed.body,
  });
}
