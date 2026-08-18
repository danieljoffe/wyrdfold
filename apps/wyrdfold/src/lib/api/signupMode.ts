import { bffSecretHeader } from '@/lib/api/bffSecret';

/**
 * Read the operator's public-signup switch from the backend (Phase 3 slice 5).
 *
 * Two callers need this and they MUST agree, because the endpoint is
 * BFF-gated:
 *  - the landing page server component, to choose waitlist vs sign-up CTA;
 *  - `/api/signup-mode`, which the login form reads for `shouldCreateUser`.
 *
 * They used to hold separate copies of this fetch, and they drifted: the
 * landing page's copy omitted {@link bffSecretHeader}, so `require_bff_secret`
 * 403'd it and the fail-safe below silently reported 'closed' — the homepage
 * could never advertise signup even with the switch flipped open (#839). One
 * shared implementation so the header can only be right or wrong in one place.
 *
 * FAIL-SAFE: every degraded state — missing env, backend down, non-2xx, junk
 * payload — reports 'closed', so the UI can never advertise signup the
 * perimeter would refuse. NB this deliberately swallows a 403, which is what
 * made #839 invisible; a 403 here means the shared secret is misconfigured
 * between Vercel and Railway, not that signup is closed. Surfacing that
 * distinction is tracked separately on #839 — it needs somewhere to report to,
 * and this helper is called from a server component.
 */
export async function readSignupMode(): Promise<'open' | 'closed'> {
  const baseUrl = process.env['WYRDFOLD_API_URL'];
  if (!baseUrl) return 'closed';
  try {
    const res = await fetch(`${baseUrl}/signup-mode`, {
      // Prove this came through the BFF (SEC-5); the API requires it here.
      headers: { ...bffSecretHeader() },
      // Perimeter flips are rare; a short cache keeps this off the hot path.
      next: { revalidate: 60 },
    });
    if (!res.ok) return 'closed';
    const data = (await res.json()) as { mode?: string };
    return data.mode === 'open' ? 'open' : 'closed';
  } catch {
    return 'closed';
  }
}
