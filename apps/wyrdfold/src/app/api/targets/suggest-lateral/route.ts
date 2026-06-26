import { LLM_TIMEOUT_MS, proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// Lateral sibling / career-stretch role suggestions for the targets the user
// is ALREADY pursuing. Distinct from the onboarding `/targets/suggest` flow
// (which proposes targets from raw experience). Mirrors that route's
// auth/proxy/error shape — see api/targets/suggest/route.ts. The API endpoint
// takes no request body (it mines the user's master payload + active targets
// server-side), so this is a bare POST proxy.
export async function POST() {
  return proxyToWyrdfoldAPI('/targets/suggest-lateral', {
    method: 'POST',
    timeoutMs: LLM_TIMEOUT_MS,
  });
}
