/**
 * POST the onboarding-complete flag, retrying once on a transient failure.
 *
 * ``fetch()`` only rejects on *network* errors — a non-2xx response (expired
 * session → 401, API down → 503, upstream 5xx) resolves normally. Callers
 * MUST therefore check the boolean returned here rather than treating
 * "didn't throw" as success. A silently-swallowed non-2xx was the root of
 * the "skip doesn't stick" bug: the wizard navigated away while
 * ``onboarding_completed_at`` stayed NULL, so the dashboard's gate bounced
 * the user straight back to /onboarding on their next visit.
 *
 * Returns ``true`` only when the flag was actually persisted (HTTP 2xx). One
 * retry absorbs a one-off blip; a 4xx short-circuits (a re-issue won't help),
 * a 5xx gets the retry.
 */
export async function completeOnboarding(): Promise<boolean> {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await fetch('/api/profile/onboarding/complete', {
        method: 'POST',
      });
      if (res.ok) return true;
      // 4xx is a protocol-level rejection — retrying just amplifies it.
      if (res.status < 500) return false;
    } catch {
      // Network / abort — fall through to the retry.
    }
    if (attempt === 0) await new Promise(r => setTimeout(r, 200));
  }
  return false;
}
