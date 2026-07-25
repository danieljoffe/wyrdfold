import type { NextRequest } from 'next/server';

/**
 * Trusted client IP for the backend's per-IP rate limiter, shared by every
 * public (unauth) BFF forwarder — the waitlist join and `/api/public/search`.
 *
 * SECURITY (frontend audit 2026-07-01, MEDIUM — do not regress):
 *  - `x-forwarded-for` is NOT a trust boundary here. On Vercel the platform
 *    APPENDS the real peer to whatever the client sent, so the LEFT-most hop is
 *    attacker-controlled — a caller can prepend a fresh fake IP per request and
 *    rotate past the backend's per-IP limit. Reading `xff[0]` forwards exactly
 *    that spoofable value.
 *  - `x-real-ip` is set by Vercel's edge to the true connecting client and
 *    overwrites any client-supplied value, so it is the trustworthy signal.
 *    (`@vercel/functions`' `ipAddress()` reads the same header; we avoid the
 *    extra dependency.)
 *  - If `x-real-ip` is absent or not a clean IP literal, we forward NOTHING and
 *    let the backend key on the connection it actually sees. Fail-closed: never
 *    forward a header we can't vouch for. Off-Vercel/local dev simply collapses
 *    onto the backend's view of the peer, which is acceptable — the rate limit
 *    is a brake, and this path is not the production trust model.
 *
 * This lives in a shared module (rather than copy-pasted per route) so the two
 * public endpoints share ONE spoof-defeating implementation — the trust logic
 * is security-critical and must not drift between forwarders.
 */

// Only accept a single, well-formed IP literal as the trusted client IP.
// Anything with a port, comma, whitespace, or junk is rejected — we forward
// nothing rather than a value we can't vouch for.
const IPV4_RE = /^(?:\d{1,3}\.){3}\d{1,3}$/;
const IPV6_RE = /^[0-9a-fA-F:]+$/;

function isPlausibleIp(value: string): boolean {
  if (IPV4_RE.test(value)) {
    return value.split('.').every(o => Number(o) <= 255);
  }
  // IPv6: at least one ':' and only hex/colon chars (loose but junk-proof).
  return value.includes(':') && IPV6_RE.test(value);
}

/**
 * Returns the Vercel-trusted `x-real-ip` when it is a clean IP literal, else
 * `''` — the caller forwards it as `x-forwarded-for` only when non-empty.
 */
export function clientIp(request: NextRequest): string {
  const realIp = request.headers.get('x-real-ip')?.trim();
  if (realIp && isPlausibleIp(realIp)) return realIp;
  return '';
}
