import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// Near-miss titles (#703 f/u): no period param — the API serves a fixed
// recent window because near-misses are actionable-now target-tuning
// signals, not trends.
export async function GET() {
  return proxyToWyrdfoldAPI('/insights/near-misses');
}
