import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// Plan + billing/BYOK state for the settings BillingCard (Phase 3). The
// upstream is saas-only and 404s on self-host — the card treats that as
// "billing not offered here" and renders nothing.
export async function GET() {
  return proxyToWyrdfoldAPI('/billing/account');
}
