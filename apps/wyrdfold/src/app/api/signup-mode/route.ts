import { NextResponse } from 'next/server';

import { bffSecretHeader } from '@/lib/api/bffSecret';

// Public perimeter probe (Phase 3 slice 5): the login form reads this to
// decide sign-in vs sign-up presentation (`shouldCreateUser`). Pre-auth by
// design — it only reveals whether signup is open, which the signup form
// itself would reveal. FAIL-SAFE: every degraded state (missing env,
// backend down, junk payload) reports 'closed', mirroring the backend
// probe and the DB hook.
export async function GET() {
  const baseUrl = process.env['WYRDFOLD_API_URL'];
  if (!baseUrl) {
    return NextResponse.json({ mode: 'closed' });
  }
  try {
    const res = await fetch(`${baseUrl}/signup-mode`, {
      // Prove this came through the BFF (SEC-5); the API requires it here.
      headers: { ...bffSecretHeader() },
      // Perimeter flips are rare; a short cache keeps this off the hot path.
      next: { revalidate: 60 },
    });
    if (!res.ok) return NextResponse.json({ mode: 'closed' });
    const data = (await res.json()) as { mode?: string };
    return NextResponse.json({
      mode: data.mode === 'open' ? 'open' : 'closed',
    });
  } catch {
    return NextResponse.json({ mode: 'closed' });
  }
}
