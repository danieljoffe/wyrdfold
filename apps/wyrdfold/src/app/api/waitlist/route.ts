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

/**
 * Best-effort client IP from proxy headers, forwarded to the backend so its
 * per-IP rate limit keys on the real visitor rather than collapsing every
 * signup onto this server's egress IP. Vercel sets `x-forwarded-for` (client
 * first in the comma list) and `x-real-ip`.
 */
function clientIp(request: NextRequest): string {
  const xff = request.headers.get('x-forwarded-for');
  if (xff) {
    const first = xff.split(',')[0]?.trim();
    if (first) return first;
  }
  return request.headers.get('x-real-ip')?.trim() || '';
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
        // Forward the real client IP so the backend's per-IP limiter sees the
        // visitor, not this server. The backend's slowapi key falls through to
        // the request's client IP for unauthenticated callers.
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
