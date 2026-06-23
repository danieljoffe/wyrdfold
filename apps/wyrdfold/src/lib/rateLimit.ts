/**
 * Minimal in-memory fixed-window rate limiter.
 *
 * Keyed by an arbitrary string (e.g. a client IP). Each key gets `limit`
 * requests per `windowMs`; the window resets on first hit after it lapses.
 * Stale entries are swept lazily on access so the map can't grow unbounded.
 *
 * SCOPE / LIMITS: state lives in module memory, so the budget is PER
 * SERVERLESS INSTANCE, not global. On a multi-instance / multi-region
 * deploy a determined attacker can get `limit × instances` through. This is
 * a deliberate, dependency-free abuse BRAKE for low-stakes public endpoints
 * (the waitlist), not a hard quota — it pairs with a DB-level unique
 * constraint that makes repeat submissions idempotent regardless. For a hard,
 * cross-instance quota, swap this for a shared store (Upstash/Redis). The
 * `RateLimiter` interface keeps that swap a one-file change.
 */

export interface RateLimitResult {
  /** Whether this request is allowed (under the limit). */
  allowed: boolean;
  /** Requests remaining in the current window after this call. */
  remaining: number;
  /** Unix-ms timestamp when the current window resets. */
  resetAt: number;
}

export interface RateLimiter {
  check(key: string): RateLimitResult;
}

interface Bucket {
  count: number;
  resetAt: number;
}

export function createRateLimiter(opts: {
  limit: number;
  windowMs: number;
}): RateLimiter {
  const { limit, windowMs } = opts;
  const buckets = new Map<string, Bucket>();

  return {
    check(key: string): RateLimitResult {
      const now = Date.now();

      // Lazy sweep: drop expired buckets so the map stays bounded by the
      // count of keys active within a single window.
      for (const [k, b] of buckets) {
        if (b.resetAt <= now) buckets.delete(k);
      }

      const existing = buckets.get(key);
      if (!existing || existing.resetAt <= now) {
        const resetAt = now + windowMs;
        buckets.set(key, { count: 1, resetAt });
        return { allowed: true, remaining: limit - 1, resetAt };
      }

      if (existing.count >= limit) {
        return { allowed: false, remaining: 0, resetAt: existing.resetAt };
      }

      existing.count += 1;
      return {
        allowed: true,
        remaining: limit - existing.count,
        resetAt: existing.resetAt,
      };
    },
  };
}

/**
 * Best-effort client IP from proxy headers. Vercel sets `x-forwarded-for`
 * (client first in the comma list) and `x-real-ip`. Falls back to a constant
 * so a missing header collapses everyone into one shared bucket (fail
 * closed-ish — stricter, never looser) rather than minting unlimited keys.
 */
export function clientIpFromHeaders(headers: Headers): string {
  const xff = headers.get('x-forwarded-for');
  if (xff) {
    const first = xff.split(',')[0]?.trim();
    if (first) return first;
  }
  return headers.get('x-real-ip')?.trim() || 'unknown';
}
