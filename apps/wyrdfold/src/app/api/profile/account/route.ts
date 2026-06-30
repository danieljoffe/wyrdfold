import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// DELETE /api/profile/account — permanent, irreversible right-to-erasure
// (#29). Proxies to the wyrdfold-api's DELETE /profile/account, which removes
// every per-user row, both storage buckets' objects under the caller's
// prefix, and the auth user. JWT-only (the upstream blocks api-key callers),
// so a user can only erase their own account. The UI gates this behind a
// typed confirmation (see DeleteAccountCard).
export async function DELETE() {
  return proxyToWyrdfoldAPI('/profile/account', { method: 'DELETE' });
}
