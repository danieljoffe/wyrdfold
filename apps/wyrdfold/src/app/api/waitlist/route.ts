import { type NextRequest, NextResponse } from 'next/server';
import * as Sentry from '@sentry/nextjs';

import { bffSecretHeader } from '@/lib/api/bffSecret';
import { pickErrorMessage } from '@/lib/api/pickErrorMessage';
import { clientIp } from '@/lib/api/clientIp';

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

// Trusted client-IP extraction (spoof-defeating `x-real-ip` read) lives in the
// shared `@/lib/api/clientIp` helper — one implementation for every public BFF
// forwarder (waitlist + `/api/public/search`).

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
        // Prove this came through the BFF so the backend accepts it (SEC-5) —
        // it rejects direct hits that could forge the IP below.
        ...bffSecretHeader(),
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
      message = pickErrorMessage(JSON.parse(text), GENERIC_ERROR);
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
