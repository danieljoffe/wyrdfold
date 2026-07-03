import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// Open the hosted Stripe Customer Portal (manage plan / card / cancel).
export async function POST() {
  return proxyToWyrdfoldAPI('/billing/portal-session', { method: 'POST' });
}
