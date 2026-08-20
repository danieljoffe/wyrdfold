import type { NextRequest } from 'next/server';

import { proxyToWyrdfoldAPI, readJsonBody } from '@/lib/api/proxy';

// Start a subscription purchase (Phase 3): forwards {plan, return_to} and
// returns the hosted Stripe Checkout URL. No card data ever touches this
// codebase.
//
// The body is forwarded WHOLE — the type parameter below only describes it, it
// does not pick fields. That matters: a BFF proxy that reconstructs the body
// field-by-field silently drops anything it wasn't updated for, which is how
// the authed salary filter lost `salary_floor` (#531). `return_to` (#887)
// reaches the API for the same reason `plan` always has.
export async function POST(request: NextRequest) {
  const parsed = await readJsonBody<{ plan?: unknown; return_to?: unknown }>(
    request
  );
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI('/billing/checkout-session', {
    method: 'POST',
    body: parsed.body,
  });
}
