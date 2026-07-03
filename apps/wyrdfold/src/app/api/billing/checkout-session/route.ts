import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

// Start a subscription purchase (Phase 3): forwards {plan} and returns the
// hosted Stripe Checkout URL. No card data ever touches this codebase.
export async function POST(request: NextRequest) {
  const parsed = await readJsonBody<{ plan?: unknown }>(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI('/billing/checkout-session', {
    method: 'POST',
    body: parsed.body,
  });
}
