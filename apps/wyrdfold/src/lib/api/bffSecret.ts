/**
 * Header proving a request came from the trusted BFF (SEC-5).
 *
 * The wyrdfold-api requires this on its public, IP-rate-limited endpoints
 * (waitlist join, signup-mode) so a direct hit to Railway can't spoof
 * `X-Forwarded-For` to rotate past the per-IP limit. Only the BFF (which holds
 * the secret) can reach those endpoints.
 *
 * Returns `{}` when `WYRDFOLD_BFF_SECRET` is unset, so a deploy that hasn't
 * configured it yet still works — the API fails open until its matching var is
 * set. Roll out by setting this (Vercel) first, then the API's (Railway).
 */
export function bffSecretHeader(): Record<string, string> {
  const secret = process.env['WYRDFOLD_BFF_SECRET'];
  return secret ? { 'x-wyrdfold-bff': secret } : {};
}
