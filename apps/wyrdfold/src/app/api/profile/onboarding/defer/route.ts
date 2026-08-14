import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// POST /api/profile/onboarding/defer — record a deliberate wizard exit
// without completing ("Finish setup later"). Leaves completed_at NULL so
// /onboarding stays enterable + resumable; the dashboard suppresses its
// auto-redirect and shows a finish-your-setup nudge instead. Idempotent.

export async function POST() {
  return proxyToWyrdfoldAPI('/profile/onboarding/defer', { method: 'POST' });
}
