import { type NextRequest, NextResponse } from 'next/server';
import * as Sentry from '@sentry/nextjs';

/**
 * Public waitlist signup (non-invited visitors on the marketing homepage).
 *
 * This route is a THIN BFF FORWARDER. The real signup happens in the
 * wyrdfold-api backend (`POST /waitlist`), which alone holds the Supabase
 * service-role key and writes the RLS deny-all `waitlist_signups` table.
 *
 * SECURITY (audit #29 — do not regress):
 *  - The frontend NO LONGER holds or uses the service-role key. Moving the
 *    write to the backend keeps `SUPABASE_SERVICE_ROLE_KEY` out of the web
 *    app's (Vercel) env entirely — narrowing audit #29 (H3) exposure rather
 *    than broadening it.
 *  - The backend is the authoritative control surface: it re-validates the
 *    email, rate-limits per client IP (slowapi), inserts ON CONFLICT DO
 *    NOTHING, and returns a generic success regardless (no enumeration) /
 *    generic error on failure. We pass its status + body straight through.
 *  - The cheap shape/length check below is a UX first layer only — it spares
 *    an obviously-bad round trip. The server is the source of truth.
 */

// Length cap matches the DB CHECK + the backend's Pydantic cap (3..320).
const MAX_EMAIL_LENGTH = 320;
const MIN_EMAIL_LENGTH = 3;

// Single `@`, non-empty local part, a dot-bearing domain, no whitespace.
// Deliberately conservative — a junk gate, not an RFC parser.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const GENERIC_ERROR = 'Something went wrong. Please try again.';

interface WaitlistBody {
  email?: unknown;
}

function isValidEmail(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length >= MIN_EMAIL_LENGTH &&
    value.length <= MAX_EMAIL_LENGTH &&
    EMAIL_RE.test(value)
  );
}

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
 * Trusted client IP for the backend's per-IP rate limiter.
 *
 * SECURITY (frontend audit 2026-07-01, MEDIUM — do not regress):
 *  - `x-forwarded-for` is NOT a trust boundary here. On Vercel the platform
 *    APPENDS the real peer to whatever the client sent, so the LEFT-most hop
 *    is attacker-controlled — a caller can prepend a fresh fake IP per request
 *    and rotate past the backend's per-IP waitlist limit. Reading `xff[0]`
 *    (the old behavior) forwarded exactly that spoofable value.
 *  - `x-real-ip` is set by Vercel's edge to the true connecting client and
 *    overwrites any client-supplied value, so it is the trustworthy signal.
 *    (`@vercel/functions`' `ipAddress()` reads the same header; we avoid the
 *    extra dependency.)
 *  - If `x-real-ip` is absent or not a clean IP literal, we forward NOTHING
 *    and let the backend key on the connection it actually sees. Fail-closed:
 *    never forward a header we can't vouch for. Off-Vercel/local dev simply
 *    collapses onto the backend's view of the peer, which is acceptable — the
 *    rate limit is a brake, and this path is not the production trust model.
 */
function clientIp(request: NextRequest): string {
  const realIp = request.headers.get('x-real-ip')?.trim();
  if (realIp && isPlausibleIp(realIp)) return realIp;
  return '';
}

export async function POST(request: NextRequest) {
  let body: WaitlistBody;
  try {
    body = (await request.json()) as WaitlistBody;
  } catch {
    return NextResponse.json(
      { error: 'Invalid request body.' },
      { status: 400 }
    );
  }

  const rawEmail =
    typeof body.email === 'string' ? body.email.trim() : body.email;
  if (!isValidEmail(rawEmail)) {
    return NextResponse.json(
      { error: 'Please enter a valid email address.' },
      { status: 400 }
    );
  }
  const email = rawEmail.toLowerCase();

  const baseUrl = process.env['WYRDFOLD_API_URL'];
  if (!baseUrl) {
    // Misconfiguration, not a client error — fail closed with a generic body.
    Sentry.captureMessage('WYRDFOLD_API_URL not configured for /api/waitlist', {
      tags: { route: 'api/waitlist' },
    });
    return NextResponse.json({ error: GENERIC_ERROR }, { status: 503 });
  }

  const ip = clientIp(request);
  try {
    const res = await fetch(`${baseUrl}/waitlist`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // Forward the TRUSTED client IP (Vercel `x-real-ip`, validated above)
        // as `x-forwarded-for` so the backend's uvicorn `--proxy-headers`
        // reads it as the peer and slowapi keys the per-IP limit on the real
        // visitor. Omitted entirely when we don't have a trustworthy IP, so we
        // never hand the backend a spoofable value.
        ...(ip ? { 'x-forwarded-for': ip } : {}),
      },
      body: JSON.stringify({ email }),
    });

    // Pass the backend's decision through verbatim: 200 generic success, 422
    // shape rejection, 429 rate-limit, 500 generic error. The backend already
    // guarantees no-enumeration / no detail leak, so we don't reinterpret it.
    const text = await res.text();
    if (res.ok) {
      return NextResponse.json({ ok: true }, { status: res.status });
    }
    // Non-2xx: relay a generic, body-shaped error. Prefer the backend's JSON
    // message when present, else the generic string. Never leak raw upstream.
    let message = GENERIC_ERROR;
    try {
      const data = JSON.parse(text) as { detail?: unknown; error?: unknown };
      const detail =
        typeof data.detail === 'string'
          ? data.detail
          : typeof data.error === 'string'
            ? data.error
            : null;
      if (detail) message = detail;
    } catch {
      // Non-JSON upstream body — keep the generic message.
    }
    const headers: Record<string, string> = {};
    const retryAfter = res.headers.get('retry-after');
    if (retryAfter) headers['Retry-After'] = retryAfter;
    return NextResponse.json(
      { error: message },
      { status: res.status, headers }
    );
  } catch (err) {
    Sentry.captureException(err, { tags: { route: 'api/waitlist' } });
    return NextResponse.json({ error: GENERIC_ERROR }, { status: 502 });
  }
}
