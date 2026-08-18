import { NextResponse } from 'next/server';

import { readSignupMode } from '@/lib/api/signupMode';

// Public perimeter probe (Phase 3 slice 5): the login form reads this to
// decide sign-in vs sign-up presentation (`shouldCreateUser`). Pre-auth by
// design — it only reveals whether signup is open, which the signup form
// itself would reveal. FAIL-SAFE: every degraded state (missing env,
// backend down, junk payload) reports 'closed', mirroring the backend
// probe and the DB hook.
//
// The probe itself lives in `lib/api/signupMode` because the landing page
// needs the identical call; keeping one copy is what stops the BFF secret
// drifting between the two callers again (#839).
export async function GET() {
  return NextResponse.json({ mode: await readSignupMode() });
}
