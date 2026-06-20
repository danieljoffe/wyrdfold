import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// BYOK key metadata + availability (#5 P4). Write-only secrets never come
// back through here — only `last4` and timestamps. Forwards the user's JWT;
// the wyrdfold-api scopes to the token subject.
export async function GET() {
  return proxyToWyrdfoldAPI('/profile/keys');
}
